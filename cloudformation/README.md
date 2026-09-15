# 四个 Quick + AgentCore 安全场景的 CloudFormation

本目录是[主博客](../docs/Quick_Connector_AgentCore_Enterprise_Apps_Security_Blog.md)对应的可审阅基础设施实现。博客解释信任边界与四种架构；本 README 是 CloudFormation 文件结构、组合关系、输入输出和部署流程的唯一详细说明。

## 1. 管理边界

这些模板管理：

- Amazon Bedrock AgentCore Gateway、execution role 和 targets；
- Quick → Gateway 所需的 Cognito M2M 身份（场景 1、2、4）；
- 业务 HTTP 认证边界所需的 REST API 或 HTTP API（场景 2、3）；
- AgentCore API key、OAuth client credentials 和 Microsoft OBO provider（场景 2、3）；
- Quick VPC Connection、Resolver、AgentCore Gateway interface endpoint及专属VPC Lambda（场景4）。

场景一、二、三不会创建、打包、更新或删除以下三只预部署业务Lambda：

- `order-tool`（场景一原生事件；场景三HTTP事件）
- `inventory-tool`
- `order-basic-auth-tool`

这些函数ARN只作为场景一、二、三的根模板参数传入。场景二、三创建的`AWS::Lambda::Permission`仅添加精确API Gateway调用statement；删除这些栈不会删除外部函数。

场景四不引用上述已部署函数，而是从相同业务源码打包两只VPC内专属Lambda，并由场景四栈独立管理其函数名、role、log group、VPC配置和生命周期。

### 1.1 业务 Lambda bootstrap

仓库级bootstrap是[`business-lambdas/deploy.py`](../business-lambdas/deploy.py)。它独立于场景栈，为场景一、二、三负责：

- 从仓库源码打包并创建/更新 `order-tool`、`inventory-tool`、`order-basic-auth-tool`；
- 为每只函数管理带ownership标签的最小IAM execution role、专属CloudWatch Logs log group和14天retention；
- 为 `order-basic-auth-tool` 创建或复用 `quick-agentcore/order-basic-auth-tool` Secrets Manager secret，并只授予该函数读取；
- 拒绝接管标签不匹配的同名资源和带Function URL的函数；
- 远端调用三只函数，验证原生、Entra delegated/app-only和Basic正负向合同；验证结果写入已被Git忽略的`evidence/`。

区域由`--region`或当前AWS profile/environment获取，账号由STS发现，不绑定任何固定环境：

```bash
: "${AWS_REGION:?Set AWS_REGION to the deployment region}"
python3 business-lambdas/deploy.py --region "$AWS_REGION"
```

可选增加`--account-id <AWS_ACCOUNT_ID>`作为误部署保护；不传时使用STS返回的当前账号。

以下资源同样不由四个场景栈管理：

- 输入的 Secrets Manager secrets；
- Microsoft Entra app registrations、consent 和 client secret；
- Quick MCP Action Connector；
- 用于 `aws cloudformation package` 的 S3 artifact bucket。

## 2. 目录结构

```text
cloudformation/
├── README.md
├── deploy.sh
├── validate_parameters.py
├── modules/
│   ├── cognito-m2m.yaml
│   ├── mixed-auth-rest-apis.yaml
│   ├── native-lambda-gateway.yaml
│   ├── private-network.yaml
│   ├── scenario4-gateway-endpoint.yaml
│   ├── scenario4-private-native-lambda-gateway.yaml
│   └── scenario4-vpc-services.yaml
├── parameters/
│   ├── scenario1.example.json
│   ├── scenario2.example.json
│   ├── scenario3.example.json
│   └── scenario4.example.json
└── scenarios/
    ├── scenario1-native-lambda.yaml
    ├── scenario2-mixed-auth.yaml
    ├── scenario3-entra-obo.yaml
    └── scenario4-private-link.yaml
```

目录职责：

| 路径 | 职责 |
|---|---|
| `scenarios/` | 可直接选择部署的四个根模板；对外定义场景参数和最终 outputs |
| `modules/` | 根模板通过 `AWS::CloudFormation::Stack` 组合的本地嵌套模板 |
| `parameters/` | `aws cloudformation deploy` 接受的非秘密参数示例；必须复制到安全位置后修改 |
| `deploy.sh` | 发现当前AWS身份、运行参数预检、package嵌套模板、部署根栈并打印非秘密outputs |
| `validate_parameters.py` | 拒绝placeholder、缺失/未知参数及partition/region/account不匹配的ARN |
| `README.md` | 模板结构、资源所有权和部署约定的主文档 |

## 3. 根模板与嵌套模块关系

```text
scenario1-native-lambda.yaml
├── cognito-m2m.yaml
└── native-lambda-gateway.yaml

scenario2-mixed-auth.yaml
├── cognito-m2m.yaml
├── mixed-auth-rest-apis.yaml       # Order SAM REST + Inventory HTTP API
├── AgentCore credential providers # 根模板内
├── AgentCore Gateway/role          # 根模板内
└── two explicit OpenAPI targets    # 根模板内

scenario3-entra-obo.yaml
└── 单根模板：HTTP APIs + JWT authorizers + Microsoft provider
             + Gateway + TOKEN_EXCHANGE OpenAPI targets

scenario4-private-link.yaml
├── cognito-m2m.yaml               # public Cognito /oauth2/token
├── private-network.yaml           # Resolver + Quick VPC Connection
├── scenario4-vpc-services.yaml    # 2 dedicated VPC business Lambdas
├── scenario4-private-native-lambda-gateway.yaml
└── scenario4-gateway-endpoint.yaml
```

嵌套依赖按CloudFormation引用自动排序。场景四先创建公共Cognito M2M身份和私网网络，再创建VPC业务Lambda、专属Gateway，最后用当前Gateway ARN创建精确policy的Gateway VPCE。根模板只重新导出Quick配置与验收需要的非秘密outputs。

### 3.1 场景一：原生 Lambda targets

根模板：`scenarios/scenario1-native-lambda.yaml`

嵌套：

- `InboundIdentity` → `modules/cognito-m2m.yaml`
- `NativeGateway` → `modules/native-lambda-gateway.yaml`

关键资源：

- Cognito user pool、resource server、client-credentials app client 和 domain；
- AgentCore `CUSTOM_JWT` Gateway；
- 只允许调用输入的两个精确 Lambda ARN 的 Gateway role；
- 两个 `GATEWAY_IAM_ROLE` native Lambda targets。

数据路径：

```text
Quick --Cognito client credentials--> AgentCore Gateway
      --Gateway IAM role--> order-tool / inventory-tool
```

### 3.2 场景二：Basic + Entra app-only OAuth 混合认证

根模板：`scenarios/scenario2-mixed-auth.yaml`

嵌套：

- `InboundIdentity` → `modules/cognito-m2m.yaml`，只负责 Quick → Gateway；
- `ServiceApis` → `modules/mixed-auth-rest-apis.yaml`，创建 Order REST 与 Inventory HTTP API。

根模板自身创建：

- external-secret API key provider；
- tenant-specific Entra `CustomOauth2` provider；
- AgentCore Gateway 和最小权限 credential role；
- 两个显式 OpenAPI targets。

`mixed-auth-rest-apis.yaml` 同时包含：

- Order `AWS::Serverless::Api`：`POST /order-requirements`，API Gateway authorization为 `NONE`，业务 Lambda校验 Basic；
- Inventory `AWS::ApiGatewayV2::Api`：payload `2.0` Lambda integration和原生 JWT authorizer；
- Inventory JWT authorizer：issuer=`https://login.microsoftonline.com/<tenant>/v2.0`，audience=`InventoryApiApplicationId`；
- Inventory route：`AuthorizationType=JWT`，不配置 `AuthorizationScopes`，因为 Entra app-only permission位于 `roles` 而非 `scp`；
- Inventory stage variables：`authorizationMode=ENTRA_APP_ONLY`、期望 tenant/client/app role；共享 `inventory-tool`只将这些可信配置与 `requestContext.authorizer.jwt.claims` 比较；
- 两条 Lambda permissions精确到账号、API、stage、method和 path。

AgentCore provider使用 tenant v2 discovery、专用 `InventoryClientApplicationId`和 external Secrets Manager secret；target用 `CLIENT_CREDENTIALS` 请求 `api://<InventoryApiApplicationId>/.default`。Inventory resource app必须定义 `InventoryReader` application app role并完成管理员 consent。

AgentCore targets不使用 REST API direct target。该 target类型不支持 Inventory所需的 OAuth client credentials；因此两个 target都使用可直接审阅输入合同的显式 OpenAPI schema。

从既有场景二栈执行更新时，CloudFormation会按当前模板替换该栈拥有的旧Inventory API/Cognito组件，并创建HTTP API/JWT authorizer；外部Entra app registrations和client-secret secret仍不归栈所有。若同一命名空间还存在命令式部署遗留的Inventory REST API或Cognito资源，必须先核对CloudFormation stack、标签和实际引用，再单独制定清理方案。

### 3.3 场景三：Entra OBO

根模板：`scenarios/scenario3-entra-obo.yaml`

该场景没有本地嵌套模板，身份与 API 资源都保留在同一个根模板中，便于同时审阅以下绑定关系：

- Order/Inventory 两个 API Gateway V2 HTTP APIs；
- 两个显式 `prod` 类 stage，不使用 `$default` stage；
- 两个 JWT authorizers：相同 tenant v2 issuer，不同 audience；
- 两条 route scopes：`Orders.Read` 和 `Inventory.Read`；
- Order API resource app 定义用户 app role `OrdersReader`，Inventory API resource app 定义 `InventoryReader`；
- 两个stage variables均固定`authorizationMode=ENTRA_DELEGATED_USER_ROLE`，分别设置`requiredUserRole=OrdersReader`/`InventoryReader`，并把`expectedAuthorizedParty`绑定到Gateway API Application ID；
- Order integration只接受预部署 `order-tool`，Inventory继续使用 `inventory-tool`；
- Microsoft OAuth provider；
- Gateway 入站 issuer、audience、`mcp.invoke`、`tid` 和 `azp` 约束；
- 两个 `OAUTH/TOKEN_EXCHANGE` OpenAPI targets，各自请求完整下游 scope URI。

Quick 手工配置使用 Entra tenant v1 authorize/token endpoints，以兼容 Quick 同时发送的 RFC 8707 `resource` 与 `scope`；Gateway 和 HTTP APIs 仍验证 resource apps 签发的 v2 access token。`Orders.Read` / `Inventory.Read` 负责 delegated API permission，`OrdersReader` / `InventoryReader` 分别负责用户业务授权。

### 3.4 场景四：Gateway PrivateLink与VPC Lambda

根模板：`scenarios/scenario4-private-link.yaml`

- `cognito-m2m.yaml`：创建独立Cognito client-credentials app client和公共user-pool domain；
- `private-network.yaml`：创建VPC、双AZ子网、Resolver、Quick VPC Connection和隔离安全组；
- `scenario4-vpc-services.yaml`：从共享源码打包两只专属Order/Inventory VPC Lambda；
- `scenario4-private-native-lambda-gateway.yaml`：创建只允许调用两只专属VPC Lambda的Gateway/targets；
- `scenario4-gateway-endpoint.yaml`：用精确Gateway ARN创建AgentCore Gateway VPCE policy；Quick元数据发现与Gateway `aws:SourceVpce` resource policy不兼容，因此不附加该策略。

Quick MCP Connector本身支持独立`AuthVpcConnectionArn`。本场景authorization-server保持Public network，是因为Cognito user-pool domain的`/oauth2/token`不支持PrivateLink；`cognito-idp` interface endpoint不接收domain OAuth流量。resource-server使用`QuickVpcConnectionArn`访问Gateway VPCE。网络无Internet Gateway或NAT Gateway，业务Lambda不需要network egress。

## 4. 共享模块接口

| 模块 | 主要输入 | 主要输出 | 使用场景 |
|---|---|---|---|
| `cognito-m2m.yaml` | Prefix、domain prefix、scope identifier/name | UserPoolId、ClientId、Scope、DiscoveryUrl、public TokenEndpoint | 1、2、4 |
| `native-lambda-gateway.yaml` | 两个外部Lambda ARN、discovery URL、allowed client/scope | Gateway ARN/ID/URL、target IDs | 1 |
| `mixed-auth-rest-apis.yaml` | Basic Order/Inventory Lambda ARN、Entra IDs、app role、stage | Order REST与Inventory HTTP API | 2 |
| `private-network.yaml` | CIDRs、resolver IP | VPC/subnets/SG、Quick connection、Resolver | 4 |
| `scenario4-vpc-services.yaml` | subnets与business Lambda SG | 两只专属VPC Lambda | 4 |
| `scenario4-private-native-lambda-gateway.yaml` | 专属Lambda ARN、JWT配置 | Gateway/targets | 4 |
| `scenario4-gateway-endpoint.yaml` | VPC/subnets/SG、Gateway ARN | 精确Gateway VPCE policy | 4 |

模块不应独立作为最终产品栈部署。根模板负责传递参数、建立跨模块依赖并重新导出稳定 outputs。

## 5. 参数组织

四个 `parameters/*.example.json` 使用以下 AWS CLI 原生格式：

```json
[
  {"ParameterKey":"Prefix","ParameterValue":"replace-me"}
]
```

参数分为五类：

| 类型 | 示例 | 规则 |
|---|---|---|
| 外部业务资源 | `OrderToolLambdaArn`、`InventoryToolLambdaArn` | 仅场景1/2/3；必须是目标context中的精确ARN |
| 入站Gateway身份 | `CognitoDomainPrefix`、`GatewayScopeIdentifier` | 场景1、2、4使用Cognito domain OAuth；场景4的token endpoint保持Public network |
| 下游凭证引用 | `OrderBasicSecretArn`、`InventoryOAuthClientSecretArn`、`GatewayOboClientSecretArn` | 只允许 secret ARN 和 JSON key，不允许 secret 值 |
| 外部 IdP 元数据 | Entra tenant/API/client application IDs与 app role | 都是非秘密控制面标识；场景二端点由 tenant确定 |
| 网络 | `QuickVpcConnectionId`、VPC/subnet CIDRs、resolver IP | 仅场景4；Quick控制面可保留已删除连接ID的墓碑，重建时使用新ID并检查CIDR/IP冲突 |

示例文件必须复制到受保护位置后修改。不要把真实 secret、token 或生成的 Cognito client secret写入参数文件。

## 6. Outputs 设计

所有根模板都输出 Quick 最后一步与控制面验收所需的非秘密值。

通用输出：

- `GatewayArn`
- `GatewayIdentifier`
- `GatewayUrl`
- `OrderTargetId`
- `InventoryTargetId`

场景 1、2、4额外输出：

- `InboundUserPoolId`
- `InboundClientId`
- `InboundScope`
- `InboundTokenEndpoint`

场景2额外输出Order REST/Inventory HTTP API与credential provider信息；场景3额外输出两个HTTP API、Microsoft provider、scopes和roles；场景4额外输出两只专属VPC Lambda ARN、`QuickVpcConnectionArn`、Gateway VPCE ID及Resolver信息。

Outputs 不包含 client secret、Basic credential、access token、refresh token 或密码。

## 7. Package 与部署流程

根模板中的本地 `TemplateURL: ../modules/*.yaml` 是源码引用，不能直接作为最终 S3 TemplateURL。`deploy.sh` 执行以下流程：

```text
parameter JSON + selected root template
      │
      ▼
STS caller account/partition + requested region
      │
      ▼
validate_parameters.py
  ├─ 拒绝placeholder、缺失/未知/重复参数
  └─ 校验Lambda/Secrets Manager ARN属于当前partition/region/account
      │
      ▼
aws cloudformation package
  ├─ 上传本地 nested templates 到 artifact bucket
  └─ 把 TemplateURL 重写为 S3 URL
      │
      ▼
aws cloudformation deploy
  ├─ CAPABILITY_NAMED_IAM
  ├─ CAPABILITY_AUTO_EXPAND
  └─ parameter-overrides file://...
      │
      ▼
describe-stacks → 打印非秘密 outputs
```

`CAPABILITY_AUTO_EXPAND`用于场景二SAM模块和场景四包含CodeUri的nested SAM application；`CAPABILITY_NAMED_IAM`用于模板内IAM资源。

### 7.1 前置条件

- AWS CLI v2 已配置到目标账号和区域，且该区域支持所选场景使用的AgentCore Gateway/Identity资源；
- 同一区域已有一个 S3 artifact bucket；
- 场景一、二、三所需的三只共享业务Lambda已部署；场景四在自己的栈内创建专属VPC Lambda；
- 场景 2/3 所需外部 IdP 和 Secrets Manager 引用已准备；
- CloudFormation execution identity 有创建模板内资源、`iam:PassRole` 和读取输入 secret 的权限；
- 场景 4 所在区域已启用 Quick，并接受 Resolver/VPCE 持续费用。

### 7.2 一条命令部署

```bash
: "${AWS_REGION:?Set AWS_REGION to the deployment region}"
cp cloudformation/parameters/scenario1.example.json /secure/path/scenario1.json
# 编辑并替换全部placeholder后：
./cloudformation/deploy.sh \
  scenario1 quick-agentcore-s1 my-cfn-artifact-bucket \
  /secure/path/scenario1.json "$AWS_REGION"
```

脚本参数依次为：

1. `scenario1|scenario2|scenario3|scenario4`
2. CloudFormation stack name
3. 已存在的 S3 artifact bucket
4. 参数 JSON 文件
5. 可选 Region；省略时必须已设置 `AWS_REGION` 或 `AWS_DEFAULT_REGION`
6. 可选 AWS profile

部署真实环境前必须复制参数示例并替换placeholder，不能直接使用`<AWS_PARTITION>`、`<AWS_REGION>`、`111122223333`、`EXAMPLE`或`replace-me`。

## 8. Secret 处理

### 8.1 场景一、二、四的入站 Cognito secret

CloudFormation 创建 confidential app client，但 Cognito CloudFormation 返回值不公开生成的 client secret。栈只输出 user pool ID 和 client ID。最后配置 Quick 时，在受保护管理员终端读取一次：

```bash
STACK_NAME=quick-agentcore-s1
USER_POOL_ID=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs[?OutputKey==`InboundUserPoolId`].OutputValue | [0]' \
  --output text)
CLIENT_ID=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs[?OutputKey==`InboundClientId`].OutputValue | [0]' \
  --output text)
aws cognito-idp describe-user-pool-client \
  --user-pool-id "$USER_POOL_ID" --client-id "$CLIENT_ID" \
  --query 'UserPoolClient.ClientSecret' --output text
```

不要把结果写入shell history、CI日志、CloudFormation output、参数文件或Git。场景一、二、四都把该secret用于Cognito domain的client-credentials token endpoint；Scenario4仅resource-server流量绑定Quick VPC Connection，auth-server保持Public network。

### 8.2 场景二的下游 secrets

Basic secret：

```json
{"base64Credentials":"<base64(username:password)>"}
```

- AgentCore API-key provider读取 `base64Credentials` 并添加 `Basic` prefix；
- `order-basic-auth-tool` 读取并验证同一个 secret；
- 参数只传`OrderBasicSecretArn`；`OrderBasicSecretJsonKey`固定为`base64Credentials`。

Inventory Entra confidential-client secret：

```json
{"clientSecret":"<inventory Entra confidential-client secret>"}
```

场景二参数只引用该 secret ARN和 JSON key。Inventory API resource app、AgentCore client app、`InventoryReader` application permission和 admin consent均在 Entra中预先完成；模板不会创建或删除这些对象。

### 8.3 场景三的 Entra secret

```json
{"clientSecret":"<Gateway API confidential-client secret>"}
```

tenant ID、四个application IDs、scopes、固定的`OrderRequiredRole=OrdersReader`和`InventoryRequiredRole=InventoryReader`是非秘密参数。两个resource API的app roles及用户/组分配在Entra中预先完成；Quick client secret只在Quick/Entra管理面处理。

场景二、三的外部Secrets Manager secret默认使用AWS managed key。若使用customer managed KMS key，必须额外评审并给实际读取主体增加精确`kms:Decrypt`权限；当前模板不会自动扩大到任意KMS key。

## 9. Quick MCP 手工最后一步

Quick MCP Action Connector 没有可用的公开 CloudFormation MCP 类型，因此不在模板中创建。

| 场景 | Quick 认证 | 网络字段 |
|---|---|---|
| 1 | Cognito client credentials | Public network |
| 2 | Cognito client credentials | Public network |
| 3 | Entra authorization code；Quick 使用 tenant v1 endpoints，access token 仍为 v2 | Public network |
| 4 | Cognito client credentials | resource-server=`QuickVpcConnectionArn`；auth-server=`Public network`（Cognito domain限制） |

所有场景均使用根栈 `GatewayUrl` 作为 MCP endpoint，并应发布恰好两个 actions。

## 10. 验证

建议在代码审阅和部署前执行：

```bash
: "${AWS_REGION:?Set AWS_REGION to the validation region}"
bash -n cloudformation/deploy.sh
cfn-lint -i W3002 \
  -t cloudformation/modules/*.yaml cloudformation/scenarios/*.yaml
sam validate --lint \
  --template-file cloudformation/modules/mixed-auth-rest-apis.yaml \
  --region "$AWS_REGION"
for template in cloudformation/modules/*.yaml cloudformation/scenarios/*.yaml; do
  aws cloudformation validate-template \
    --region "$AWS_REGION" \
    --template-body "file://$PWD/$template" >/dev/null
done
git diff --check
```

`W3002` 仅针对源码中的本地 nested `TemplateURL`；`deploy.sh` 会通过 `aws cloudformation package` 把它们替换成 S3 URL。不要忽略其他 lint 错误。

还应检查：

- 所有参数示例覆盖根模板中无默认值的参数；
- 场景一、二、三根模板不创建业务Lambda；Scenario4仅在`scenario4-vpc-services.yaml`创建专属VPC Lambda；
- 场景 2 两个 AgentCore targets都是显式 OpenAPI targets；
- 场景 2 Inventory route没有 `AuthorizationScopes`，stage variables与 Lambda代码共同强制 `tid`、`azp/appid`、`roles`；
- 场景 3 Order/Inventory routes分别要求 `Orders.Read` / `Inventory.Read`，stage variables与业务 Lambda共同强制可信 `roles` 包含 `OrdersReader` / `InventoryReader`；
- 场景 3正式验收使用两个不同 Entra 用户：full-access 用户分配两个 Reader roles并成功访问两项服务；inventory-only 用户只分配 `InventoryReader`，Order 403且 Inventory成功；
- 内嵌 OpenAPI JSON可解析，required 字段与业务工具合同一致；
- outputs 名称和值不包含 secret、password 或 token；
- Gateway 默认不设置 `ExceptionLevel: DEBUG`。

## 11. 删除与资源所有权

```bash
aws cloudformation delete-stack --stack-name quick-agentcore-s1
aws cloudformation wait stack-delete-complete --stack-name quick-agentcore-s1
```

删除根栈会递归删除 nested stacks 和场景拥有的 IAM、Gateway、targets、API、Cognito 或网络资源，但不会删除：

- 场景一、二、三引用的三只外部业务Lambda；场景四的专属VPC Lambda属于其根栈并会随栈删除；
- 输入的 Secrets Manager secrets；
- 外部 Cognito/Entra 配置；
- 手工创建的 Quick MCP Connector；
- S3 artifact bucket。

场景4删除前先删除或解绑Quick Connector；测试结束后及时删除根栈中的Resolver endpoint和Gateway interface VPCE，避免持续费用。

## 12. 推荐审阅顺序

1. 从 `scenarios/scenarioN-*.yaml` 查看场景边界、参数与最终 outputs；
2. 沿 `TemplateURL` 进入对应 `modules/*.yaml`；
3. 核对 IAM policy 是否精确限制到 Lambda、provider、secret、API 或 Gateway ARN；
4. 核对 OpenAPI tool schema 与业务函数输入合同；
5. 检查 `parameters/scenarioN.example.json` 没有 secret 值；
6. 最后检查 `deploy.sh` 的 package、capabilities 和参数传递。
