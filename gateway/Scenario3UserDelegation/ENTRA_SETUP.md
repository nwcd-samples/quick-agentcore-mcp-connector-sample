# Scenario 3 Microsoft Entra ID标准OBO配置指南

本指南配置以下链路：

```text
Quick用户
  → Entra authorization code（Gateway API: mcp.invoke）
  → AgentCore Gateway CUSTOM_JWT
  → AgentCore Identity MicrosoftOauth2 OBO / TOKEN_EXCHANGE
  → Order / Inventory HTTP APIs及共享业务Lambda
```

基础设施只通过[`cloudformation/scenarios/scenario3-entra-obo.yaml`](../../cloudformation/scenarios/scenario3-entra-obo.yaml)部署。不要把client secret、access token或refresh token写入Git、CloudFormation参数值、output或日志。

## 1. 准备区域和回调

```bash
: "${AWS_REGION:?Set AWS_REGION to the deployment region}"
export REGION="$AWS_REGION"
export PREFIX=quick-agentcore-s3
export QUICK_CALLBACK="https://${REGION}.quicksight.aws.amazon.com/sn/oauthcallback"
```

所有Entra应用必须属于同一个tenant并使用单租户账号类型。Application ID均指Application (client) ID，不是Object ID。

## 2. 创建四个互不相同的App Registration

| 应用 | 必需配置 |
|---|---|
| Gateway API | v2 access token；Application ID URI；delegated scope `mcp.invoke`；client secret |
| Quick Client | confidential Web client；Quick callback；Gateway `mcp.invoke` permission；独立secret |
| Order API | v2 access token；delegated scope `Orders.Read`；Users/Groups app role `OrdersReader` |
| Inventory API | v2 access token；delegated scope `Inventory.Read`；Users/Groups app role `InventoryReader` |

CloudFormation会拒绝格式不正确或重复的四个Application ID。

### 2.1 Gateway API

1. 设置`api.requestedAccessTokenVersion=2`；
2. 设置Application ID URI：`api://<GATEWAY_API_APPLICATION_ID>`；
3. 创建并启用delegated scope `mcp.invoke`；
4. 创建Gateway API自己的client secret并保存**Value**；
5. 添加Order `Orders.Read`和Inventory `Inventory.Read`两项delegated permissions并完成admin consent。

Gateway API同时是入站resource和OBO middle tier client。该secret不能与Quick Client secret混用。

### 2.2 Order API

1. 设置`api.requestedAccessTokenVersion=2`；
2. 设置Application ID URI：`api://<ORDER_API_APPLICATION_ID>`；
3. 创建delegated scope `Orders.Read`；
4. 创建app role：value=`OrdersReader`、Allowed member types=`Users/Groups`、Enabled。

### 2.3 Inventory API

1. 设置`api.requestedAccessTokenVersion=2`；
2. 设置Application ID URI：`api://<INVENTORY_API_APPLICATION_ID>`；
3. 创建delegated scope `Inventory.Read`；
4. 创建app role：value=`InventoryReader`、Allowed member types=`Users/Groups`、Enabled。

### 2.4 Quick Client

1. 添加Web callback：`https://<REGION>.quicksight.aws.amazon.com/sn/oauthcallback`；
2. 不启用implicit grant；
3. 添加Gateway API的delegated `mcp.invoke`并按组织策略consent；
4. 创建Quick Client自己的secret；该Value只输入Quick Connector。

## 3. 分配测试用户业务角色

| 用户 | Order API | Inventory API | 预期 |
|---|---|---|---|
| full-access | `OrdersReader` | `InventoryReader` | 两工具成功 |
| inventory-only | 不分配 | `InventoryReader` | Order业务授权拒绝；Inventory成功 |

角色必须在对应resource API的Enterprise Application上分配。为了验证Lambda自身的Order拒绝分支，测试环境不要让Order Enterprise Application的`Assignment required`在OBO阶段提前拒绝inventory-only用户。

## 4. 将Gateway OBO secret保存到AWS Secrets Manager

CloudFormation要求`SecretString`只包含`clientSecret`字段。下面示例通过`getpass`读取Value，不把值放进命令行或文件；同名secret存在时创建新version：

```bash
export ENTRA_GATEWAY_SECRET_ID="${PREFIX}/entra-gateway-obo-client-secret"
python3 - <<'PY'
import boto3, getpass, json, os

client = boto3.client("secretsmanager", region_name=os.environ["REGION"])
secret_id = os.environ["ENTRA_GATEWAY_SECRET_ID"]
value = getpass.getpass("Gateway API OBO client secret Value: ")
if not value:
    raise SystemExit("empty secret")
payload = json.dumps({"clientSecret": value}, separators=(",", ":"))
try:
    response = client.create_secret(
        Name=secret_id,
        Description="Scenario 3 Entra Gateway API OBO client secret",
        SecretString=payload,
    )
    secret_arn = response["ARN"]
except client.exceptions.ResourceExistsException:
    client.put_secret_value(SecretId=secret_id, SecretString=payload)
    secret_arn = client.describe_secret(SecretId=secret_id)["ARN"]
value = payload = ""
print(f"Gateway OBO secret ARN: {secret_arn}")
PY
```

记录ARN。示例IAM只包含`secretsmanager:GetSecretValue`；请使用Secrets Manager默认AWS managed key。若组织要求customer managed KMS key，需在部署前单独审阅并增加精确`kms:Decrypt`权限。

## 5. 准备CloudFormation参数

```bash
cp cloudformation/parameters/scenario3.example.json /secure/path/scenario3.json
chmod 600 /secure/path/scenario3.json
```

填写：

| 参数 | 来源 |
|---|---|
| `OrderToolLambdaArn` / `InventoryToolLambdaArn` | `business-lambdas/deploy.py`部署结果 |
| `EntraTenantId` | Directory tenant ID |
| `GatewayApiApplicationId` | Gateway API Application ID |
| `QuickClientApplicationId` | Quick Client Application ID |
| `OrderApiApplicationId` | Order API Application ID |
| `InventoryApiApplicationId` | Inventory API Application ID |
| `GatewayInboundScope` | `mcp.invoke` |
| `OrderRequiredRole` | `OrdersReader` |
| `InventoryRequiredRole` | `InventoryReader` |
| `GatewayOboClientSecretArn` | 第4步输出的Secrets Manager ARN |
| `GatewayOboClientSecretJsonKey` | `clientSecret` |

参数文件只含非秘密ID和secret ARN，不含secret Value。

## 6. 部署

```bash
python3 business-lambdas/deploy.py --region "$REGION"
./cloudformation/deploy.sh \
  scenario3 quick-agentcore-s3 <ARTIFACT_BUCKET> \
  /secure/path/scenario3.json "$REGION"
```

确认outputs和控制面：Gateway、两个targets READY；Microsoft provider引用正确secret；Order/Inventory分别使用自己的audience、scope和Reader role；stage的`expectedAuthorizedParty`均为Gateway API Application ID。

## 7. 配置Quick Connector

| Quick字段 | 值 |
|---|---|
| MCP endpoint | CloudFormation `GatewayUrl` |
| Authentication | OAuth 2.0 Authorization Code |
| Authorization URL | `https://login.microsoftonline.com/<TENANT_ID>/oauth2/authorize` |
| Token URL | `https://login.microsoftonline.com/<TENANT_ID>/oauth2/token` |
| Client ID | Quick Client Application ID |
| Client secret | Quick Client secret **Value** |
| Scope | `openid profile offline_access api://<GATEWAY_API_APPLICATION_ID>/mcp.invoke` |
| Redirect URL | Quick callback URL |

若Quick携带RFC 8707 `resource`时v2 endpoint返回`AADSTS9010010`，使用上表tenant v1 authorize/token endpoints；resource apps仍签发并由Gateway/API验证v2 access token。

## 8. 验证双用户矩阵

先运行本地合同测试：

```bash
python3 -m unittest -v \
  tests.test_scenario3_user_delegation \
  tests.test_predeployed_business_lambdas
```

然后分别使用full-access和inventory-only用户在Quick完成OAuth与工具调用，确认：

- full-access用户的Order与Inventory均成功；
- inventory-only用户的Order返回`ORDERS_READER_ROLE_REQUIRED`且不返回订单数据，Inventory成功；
- 错误tenant、audience、scope或OBO actor被拒绝；
- `ORDER-1001`得到`SHORTAGE_RISK`且缺货数量为2；
- 不保存token、secret或用户Object ID。

## 9. 常见错误

| 现象 | 检查项 |
|---|---|
| `AADSTS7000215` | Gateway secret是Value、未过期且Secrets Manager JSON key为`clientSecret` |
| `AADSTS65001` | Gateway API两个下游delegated permissions均已consent |
| Gateway 401 | tenant、Gateway audience、`mcp.invoke`、Quick Client `azp` |
| 下游401/403 | API audience、route scope、Gateway API `azp/appid`和Reader role |
| token `ver=1.0` | 三个resource app均设置requestedAccessTokenVersion=2 |
| `AADSTS9010010` | Quick改用tenant v1 authorize/token endpoints |
