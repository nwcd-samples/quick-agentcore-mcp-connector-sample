# Quick Two-Tool Demo业务合同

本目录提供三只共享业务Lambda中的两个核心只读工具源码：

```text
<Prefix>-order-requirements___get_order_requirements
  → order-tool
<Prefix>-inventory-availability___check_inventory_availability
  → inventory-tool
```

`<Prefix>-...`由当前CloudFormation栈决定；稳定合同是operation ID `get_order_requirements`和`check_inventory_availability`，不要依赖旧的固定action前缀。

## 调用顺序

1. Quick先调用订单工具，获得聚合后的`order_id`、`requirements`和`requirements_checksum`；
2. 仅当订单返回`SUCCESS`时，原样把三项结果传给库存工具；
3. 库存工具验证checksum后计算短缺和风险。

两个Lambda没有Function URL、ALB、公网IP或独立写接口。Gateway IAM只允许调用输入的两个精确Lambda ARN；函数role只写自己的日志组。

## 四场景事件合同

- 场景1/4：原生Lambda target事件；
- 场景2：`inventory-tool`接收HTTP API JWT事件，stage固定`ENTRA_APP_ONLY`并校验tenant、client和`InventoryReader`；
- 场景3：两个工具接收HTTP API JWT事件，stage固定`ENTRA_DELEGATED_USER_ROLE`，校验Reader role；现行CloudFormation还用`expectedAuthorizedParty`绑定Gateway API OBO actor。

`inventory-tool`另外保留两个明确的旧集成兼容面：API Gateway REST/Cognito authorizer context，以及“无stage variables但由route delegated scope完成业务授权”的JWT HTTP事件。它们不被四个现行CloudFormation场景使用，但由回归测试保护，不能在未完成旧消费者迁移前直接删除。app-only token没有`scp`，缺stage时仍会fail closed。

## Demo订单

| Order | 预期结果 |
|---|---|
| `ORDER-1001` | 重复行聚合；`SKU-RED/WH-A`缺货2；`SHORTAGE_RISK` |
| `ORDER-1002` | 全部可用；`NO_SHORTAGE_FOUND` |
| `ORDER-1003` | 零库存；缺货6；`SHORTAGE_RISK` |
| `ORDER-1004` | 一个库存源模拟失败；`PARTIAL/UNABLE_TO_FULLY_ASSESS` |
| 其他合法ID | `ORDER_NOT_FOUND`；不得继续调用库存工具 |

所有记录都是固定的非客户Demo数据。

## Schema说明

AgentCore native Lambda target当前inline schema只接受`type`、`properties`、`required`、`items`和`description`等有限字段；`tool-schema.json`保留完整Draft 7合同，Lambda本身继续拒绝未知字段、非法格式/数量、重复key和checksum不匹配。
