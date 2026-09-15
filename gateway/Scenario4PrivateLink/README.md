# Scenario 4 — Gateway PrivateLink + VPC Lambda

场景四把Quick到AgentCore Gateway的resource-server流量放入Quick VPC Connection和AWS PrivateLink，同时把Order、Inventory两只专属Lambda部署在场景VPC内。OAuth token请求继续访问公共Cognito user-pool domain。

产品化入口是[`cloudformation/scenarios/scenario4-private-link.yaml`](../../cloudformation/scenarios/scenario4-private-link.yaml)。

## 为什么auth-server保持Public network

Amazon Quick MCP Connector本身支持独立的auth-server VPC connection（`AuthVpcConnectionArn`），可以把OAuth discovery、authorization和token流量送入客户VPC。**本场景不能把auth endpoint放进PrivateLink不是Quick的限制，而是Amazon Cognito user-pool domain的限制。**

Cognito官方明确说明：

- user-pool domain的`/oauth2/token`、managed login和hosted UI不支持Cognito PrivateLink；
- `com.amazonaws.<region>.cognito-idp` interface endpoint只支持Cognito API操作，不接收user-pool domain OAuth请求；
- private DNS不会把`<domain>.auth.<region>.amazoncognito.com`路由到Cognito VPCE。

参见[AWS Cognito PrivateLink限制](https://docs.aws.amazon.com/cognito/latest/developerguide/vpc-interface-endpoints.html)和[Quick MCP resource/auth双VPC连接能力](https://docs.aws.amazon.com/quick/latest/userguide/mcp-integration.html)。

因此本场景保持：

```text
Resource-server connection = QuickVpcConnectionArn
Auth-server connection     = Public network
Token URL                  = Cognito InboundTokenEndpoint
```

如果将来改用原生提供private标准OAuth endpoint的授权服务器，可以直接使用Quick的`AuthVpcConnectionArn`，无需修改Quick MCP协议。

## 架构

```text
Amazon Quick MCP Connector
  │
  ├─ OAuth client credentials
  │    → Public Cognito /oauth2/token
  │
  └─ Resource-server Quick VPC connection
       → Route 53 Resolver inbound endpoint
       → AgentCore Gateway interface VPCE
       → AgentCore Gateway CUSTOM_JWT
       ├→ dedicated order-vpc-tool Lambda（VPC内）
       └→ dedicated inventory-vpc-tool Lambda（VPC内）
```

PrivateLink只改变Quick到Gateway的数据路径，不替代OAuth/JWT、TLS或Gateway最小权限IAM。AgentCore标准`GatewayUrl`仍是公网可解析的AWS托管hostname，公网无token请求可能到达服务并返回401/403；Scenario4依赖Cognito JWT authorizer、Gateway VPCE security group和精确endpoint policy保护私网连接。当前Quick元数据发现与Gateway `aws:SourceVpce` resource policy不兼容，因此模板不附加该策略。

## Scenario 4专属资源

根栈创建并拥有：

- 独立Cognito user pool、resource server、confidential app client和domain；
- 独立AgentCore Gateway、execution role和两个native Lambda targets；
- 两个AZ的VPC、私有子网和Route 53 Resolver inbound endpoint；
- Quick、Resolver、Gateway VPCE和业务Lambda独立安全组；
- `com.amazonaws.<region>.bedrock-agentcore.gateway` interface endpoint；
- `AWS::QuickSight::VPCConnection`；
- VPC内专属`<Prefix>-order-vpc-tool`与`<Prefix>-inventory-vpc-tool`。

业务Lambda从共享源码打包，但使用独立函数名、execution role、log group、VPC配置和生命周期。场景一、二、三仍使用预部署`order-tool`、`inventory-tool`，不受Scenario4部署或删除影响。

## 输入与部署

Scenario4不接收外部业务Lambda ARN；它仍需要区域内唯一的Cognito domain prefix：

```bash
: "${AWS_REGION:?Set AWS_REGION to the deployment region}"
cp cloudformation/parameters/scenario4.example.json /secure/path/scenario4.json
./cloudformation/deploy.sh \
  scenario4 quick-agentcore-s4 <ARTIFACT_BUCKET> \
  /secure/path/scenario4.json "$AWS_REGION"
```

关键参数：

| 参数 | 说明 |
|---|---|
| `Prefix` | Scenario4专属资源名前缀 |
| `CognitoDomainPrefix` | 区域内唯一的公共Cognito domain prefix |
| `GatewayScopeIdentifier` | Gateway resource-server identifier |
| `QuickVpcConnectionId` | Quick VPC Connection的唯一ID；每次重新创建场景四都必须使用未被Quick控制面保留的全新值 |
| `VpcCidr` | 专属VPC CIDR |
| `SubnetCidrA/B` | 两个不同AZ的私有子网CIDR |
| `ResolverIpAddressA/B` | 对应子网内未占用的Resolver固定IP |

部署前确认CIDR和固定IP无冲突，目标区域支持Quick VPC Connection及AgentCore Gateway PrivateLink。`QuickVpcConnectionId`不能复用近期删除的值：Quick控制面可能保留`DELETED`墓碑及其旧VPC子网关联，复用时会报“Existing VPCConnection resource has subnets associated with VPC”。例如可使用：

```text
quick-agentcore-s4-vpc-20260915b
```

## 关键outputs

- `GatewayArn`、`GatewayIdentifier`、`GatewayUrl`；
- `OrderTargetId`、`InventoryTargetId`；
- `OrderVpcLambdaArn`、`InventoryVpcLambdaArn`；
- `InboundUserPoolId`、`InboundClientId`、`InboundScope`、`InboundTokenEndpoint`；
- `QuickVpcConnectionArn`；
- `GatewayVpcEndpointId`；
- `ResolverEndpointId`与resolver IPs。

## Quick MCP Connector配置

CloudFormation不创建Quick Connector。管理员读取一次Cognito client secret，并填写：

| 字段 | 值 |
|---|---|
| MCP endpoint | `GatewayUrl` |
| Authentication | OAuth 2.0 client credentials |
| Client ID | `InboundClientId` |
| Client secret | 对应Cognito app client secret Value |
| Token URL | `InboundTokenEndpoint` |
| Scope | `InboundScope` |
| Resource-server VPC connection | `QuickVpcConnectionArn` |
| Auth-server VPC connection | **Public network** |

不要把Cognito token URL错误绑定到`AuthVpcConnectionArn`；该domain不能通过Cognito interface endpoint访问。这一限制来自Cognito，不代表Quick不支持private auth provider。

## 网络安全合同

```text
Quick ENI SG → Resolver SG: TCP/UDP 53
Quick ENI SG → Gateway VPCE SG: TCP 443
Business Lambda SG: no ingress, no egress
```

Gateway VPCE policy只允许对当前Gateway ARN执行`bedrock-agentcore:InvokeGateway`。当前Quick MCP元数据发现与Gateway `aws:SourceVpce` resource policy不兼容：附加显式Deny会使连接器创建时出现`CONNECTION_TIMEOUT`，因此模板不附加该策略。网络没有Internet Gateway或NAT Gateway；业务Lambda使用固定内存Demo数据，不需要网络egress。

## 验证

```bash
python3 -m unittest -v tests.test_scenario4_privatelink
```

部署后至少确认：

1. Gateway VPCE为`available`且private DNS开启；
2. Quick VPC Connection为`AVAILABLE`；
3. Gateway与两个targets为`READY`；
4. 两只Scenario4 Lambda均配置两个private subnet及专属security group；
5. Connector的`VpcConnectionArn`为`QuickVpcConnectionArn`；
6. Connector的auth-server connection保持Public network；
7. 公网无token请求被JWT authorizer拒绝；
8. Connector发布恰好两个只读actions，并由实际Quick用户完成Order/Inventory调用；
9. Connector创建失败时优先检查Resolver、VPCE、SG和endpoint policy；不要附加`aws:SourceVpce` Gateway resource policy。

## 删除边界

先删除或解绑Quick Connector，再删除Scenario4根栈。根栈会删除专属Gateway、targets、Cognito、两只VPC Lambda、Resolver、Gateway VPCE、Quick VPC Connection、安全组和IAM roles；不会删除场景一、二、三资源、artifact bucket或手工创建的Quick Connector。

Resolver和Gateway interface endpoint会持续计费，测试完成后应及时删除栈。
