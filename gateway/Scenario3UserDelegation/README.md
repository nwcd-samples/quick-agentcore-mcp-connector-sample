# Scenario 3 — Entra用户委托与标准OBO

场景三把Quick中通过Entra authorization code认证的终端用户身份，经AgentCore Identity标准Microsoft OBO传给订单和库存API。

## 架构与授权合同

```text
Quick user token (aud=Gateway API, scp=mcp.invoke, azp=Quick Client)
  → AgentCore Gateway CUSTOM_JWT
  → MicrosoftOauth2 OBO / TOKEN_EXCHANGE (client=Gateway API)
  ├→ Order HTTP API (aud=Order API, scp=Orders.Read)
  │   → order-tool: azp/appid=Gateway API + roles contains OrdersReader
  └→ Inventory HTTP API (aud=Inventory API, scp=Inventory.Read)
      → inventory-tool: azp/appid=Gateway API + roles contains InventoryReader
```

`Orders.Read`/`Inventory.Read`是delegated permissions；`OrdersReader`/`InventoryReader`是对应resource API定义并分配给用户或组的业务角色。scope和role不能互相替代。两个Lambda只信任API Gateway写入的JWT claims和CloudFormation控制的stage variables；普通header、query、body或tool argument不能提供身份和角色。

场景三不使用自建token-exchange Lambda/API、KMS签名key、actor secret或Lambda authorizer。

## 唯一部署路径

基础设施唯一控制器是[`cloudformation/scenarios/scenario3-entra-obo.yaml`](../../cloudformation/scenarios/scenario3-entra-obo.yaml)。仓库不再保留另一套命令式部署器或依赖私有state schema的验证器，避免同一环境生成两套Gateway、API和provider。

完整Entra对象、consent、secret和Quick字段配置见[ENTRA_SETUP.md](ENTRA_SETUP.md)。

先部署三只共享业务Lambda：

```bash
: "${AWS_REGION:?Set AWS_REGION to the deployment region}"
python3 business-lambdas/deploy.py --region "$AWS_REGION"
```

复制参数示例到仓库外受保护位置，填入四个互不相同的Application ID和Secrets Manager ARN：

```bash
cp cloudformation/parameters/scenario3.example.json /secure/path/scenario3.json
./cloudformation/deploy.sh \
  scenario3 quick-agentcore-s3 <ARTIFACT_BUCKET> \
  /secure/path/scenario3.json "$AWS_REGION"
```

Gateway API OBO secret的Secrets Manager `SecretString`必须采用唯一schema：

```json
{"clientSecret":"<Gateway API confidential-client secret Value>"}
```

参数`GatewayOboClientSecretJsonKey`固定为`clientSecret`。Quick Client secret只输入Quick，不写入仓库、参数、output或日志。

## 验证

本地合同测试：

```bash
python3 -m unittest -v \
  tests.test_scenario3_user_delegation \
  tests.test_predeployed_business_lambdas
```

部署后从CloudFormation outputs和AWS控制面确认：

- Gateway和两个targets均为READY；
- Gateway入站约束tenant、Gateway audience、`mcp.invoke`和Quick Client `azp`；
- 两个HTTP API使用相同tenant v2 issuer但不同audience；
- routes分别要求`Orders.Read`和`Inventory.Read`；
- stages分别要求`OrdersReader`和`InventoryReader`，并将`expectedAuthorizedParty`固定为Gateway API Application ID；
- 两个targets均使用同一Microsoft provider和`TOKEN_EXCHANGE`，但请求不同的完整scope URI。

正式验收使用两个不同Entra用户：

| 用户 | Order | Inventory |
|---|---|---|
| 同时分配`OrdersReader`与`InventoryReader` | 成功 | 成功 |
| 只分配`InventoryReader` | 403 / `ORDERS_READER_ROLE_REQUIRED` | 成功 |

用户token和Quick Desktop端到端结果只能通过受批准的短期流程和人工验收处理，不应写入Git或长期evidence文件。
