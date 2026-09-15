# Scenario 2 — Basic + Entra app-only OAuth

订单系统模拟 legacy HTTP Basic 认证；库存系统使用 Microsoft Entra ID client credentials、API Gateway HTTP API 原生 JWT authorizer和可信 app-only claims授权。Quick → AgentCore Gateway由独立 Cognito client credentials保护。

## 架构

```text
AgentCore Gateway
  ├─ Order target / API_KEY provider
  │   → Authorization: Basic ...
  │   → Order REST API (authorizationType=NONE)
  │   → order-basic-auth-tool: Secrets Manager Basic validation + business logic
  └─ Inventory target / CustomOauth2 CLIENT_CREDENTIALS
      → Entra tenant v2 token endpoint
      → scope=api://<InventoryApiApplicationId>/.default
      → Inventory HTTP API native JWT (issuer + audience)
      → inventory-tool: trusted tid + azp/appid + InventoryReader role
```

Entra app-only permissions位于 `roles`，不是 `scp`。HTTP API JWT authorizer负责签名、issuer、audience和时间 claims；`inventory-tool` 只读取 `requestContext.authorizer.jwt.claims`，并与受部署控制的 stage variables比较。普通 header、query和body不能提供身份。

## 唯一部署路径

本场景不再保留会创建另一套 `bieases-s2-mixed-*` Lambda、secret、API和AgentCore资源的 `deploy_scenario2.py`。基础设施唯一实现为：

- [`cloudformation/scenarios/scenario2-mixed-auth.yaml`](../../cloudformation/scenarios/scenario2-mixed-auth.yaml)
- [`cloudformation/modules/mixed-auth-rest-apis.yaml`](../../cloudformation/modules/mixed-auth-rest-apis.yaml)

三只业务 Lambda由仓库级 bootstrap独立管理，场景栈只接收 ARN并添加精确的调用权限：

```bash
: "${AWS_REGION:?Set AWS_REGION to the deployment region}"
python3 business-lambdas/deploy.py --region "$AWS_REGION"
```

`--account-id <AWS_ACCOUNT_ID>` 是可选的误部署保护，不是固定环境要求。bootstrap使用STS发现当前账号；详细职责见 [`cloudformation/README.md`](../../cloudformation/README.md#11-业务-lambda-bootstrap)。

复制并编辑参数示例，然后部署：

```bash
cp cloudformation/parameters/scenario2.example.json /secure/path/scenario2.json
./cloudformation/deploy.sh \
  scenario2 quick-agentcore-s2 <ARTIFACT_BUCKET> \
  /secure/path/scenario2.json "$AWS_REGION"
```

## Entra 前置配置

需要两个不同的 app registrations：

1. Inventory API：Application ID URI=`api://<api-app-id>`，v2 access token，定义 `InventoryReader` app role（Allowed member types=`Applications`），Enterprise Application启用 Assignment required；
2. AgentCore Inventory client：添加 Inventory API 的 `InventoryReader` application permission，完成 admin consent，并创建 client secret。

CloudFormation参数传入tenant ID、两个Application ID、secret ARN和JSON key；模板不创建或删除Entra对象。

## Secrets Manager schema

订单Basic secret由`business-lambdas/deploy.py`创建或复用；`OrderBasicSecretArn`必须使用bootstrap输出的同一个ARN，不能另建第二份凭据。schema：

```json
{"base64Credentials":"<base64(username:password)>"}
```

库存 Entra confidential-client secret：

```json
{"clientSecret":"<Entra confidential-client secret Value>"}
```

两个值都存入AWS Secrets Manager。参数文件只保存ARN；`OrderBasicSecretJsonKey`固定为`base64Credentials`，`InventoryOAuthClientSecretJsonKey`固定为`clientSecret`。AgentCore provider和业务Lambda在运行时读取引用；secret、Basic header和access token不会进入CloudFormation output、state或Git。

示例IAM按Secrets Manager默认AWS managed key设计；customer managed KMS key需要另行增加并评审精确`kms:Decrypt`权限。

## 验证

```bash
python3 -m unittest -v \
  tests.test_scenario2_mixed_auth \
  tests.test_predeployed_business_lambdas
```

部署后按 [`cloudformation/README.md`](../../cloudformation/README.md) 的控制面与手工Connector步骤验证：两个target READY；Inventory route没有route scope但强制tenant、client和`InventoryReader`；匿名及错误凭据被拒绝；`ORDER-1001`得到确定性的`SHORTAGE_RISK`和缺货数量2。
