# Quick + AgentCore MCP Connector安全场景示例

本仓库演示Amazon Quick MCP Connector通过Amazon Bedrock AgentCore Gateway安全调用两个只读业务工具，并分别覆盖服务身份、legacy Basic、Microsoft Entra应用身份、用户OBO和PrivateLink网络边界。

## 当前交付内容

| 场景 | Quick → Gateway | Gateway → 业务 | 根模板 |
|---|---|---|---|
| 1 | Cognito client credentials | Gateway IAM → native Lambda | `scenario1-native-lambda.yaml` |
| 2 | Cognito client credentials | Order Basic；Inventory Entra app-only | `scenario2-mixed-auth.yaml` |
| 3 | Entra authorization code | Microsoft Entra OBO | `scenario3-entra-obo.yaml` |
| 4 | Cognito client credentials；Gateway走Quick VPC Connection | Gateway IAM → dedicated VPC Lambda | `scenario4-private-link.yaml` |

四个场景的基础设施唯一由[`cloudformation/`](cloudformation/)管理；Quick MCP Connector是部署后的手工步骤。场景二、三、四不再保留另一套命令式基础设施控制器。

Scenario4只把MCP resource-server路径放入PrivateLink。Gateway VPCE security group和endpoint policy限制Quick VPC Connection到当前Gateway的私网路径；Gateway仍由Cognito JWT authorizer保护。标准Gateway hostname仍公网可解析，公网无token请求可能返回401/403。当前Quick元数据发现与Gateway `aws:SourceVpce` resource policy不兼容，因此模板不附加该策略；不要将VPCE endpoint policy误认为能关闭托管服务的公网hostname。Quick本身支持独立`AuthVpcConnectionArn`，Cognito user-pool domain的`/oauth2/token`不支持PrivateLink，因此authorization-server保持Public network。

## 三只共享业务Lambda

[`business-lambdas/deploy.py`](business-lambdas/deploy.py)是独立bootstrap，为场景一、二、三创建或更新：

- `order-tool`
- `inventory-tool`
- `order-basic-auth-tool`

它同时管理regional IAM execution roles、日志组和Order Basic Secrets Manager secret，并执行远端正负向合同验证。账号通过STS发现，区域来自CLI或AWS配置。Scenario4不复用这些已部署函数，而是在自己的栈内从相同源码创建VPC专属副本。

```bash
: "${AWS_REGION:?Set AWS_REGION to the deployment region}"
python3 business-lambdas/deploy.py --region "$AWS_REGION"
```

## 部署一个场景

所有命令从仓库根目录执行：

```bash
cp cloudformation/parameters/scenario1.example.json /secure/path/scenario1.json
# 替换全部placeholder后：
./cloudformation/deploy.sh \
  scenario1 quick-agentcore-s1 <ARTIFACT_BUCKET> \
  /secure/path/scenario1.json "$AWS_REGION"
```

`deploy.sh`会在package之前拒绝placeholder、缺失/未知参数，以及partition、region或account与当前AWS调用身份不一致的Lambda/Secrets Manager ARN。Scenario4重建时还必须为`QuickVpcConnectionId`填写新的唯一值，避免Quick删除墓碑冲突。

## 文档

- [CloudFormation结构、参数、outputs和删除边界](cloudformation/README.md)
- [完整架构与四场景说明](docs/Quick_Connector_AgentCore_Enterprise_Apps_Security_Blog.md)
- [Scenario 2：Basic + Entra app-only](gateway/Scenario2MixedAuth/README.md)
- [Scenario 3：Entra OBO](gateway/Scenario3UserDelegation/README.md)
- [Scenario 3 Entra配置指南](gateway/Scenario3UserDelegation/ENTRA_SETUP.md)
- [Scenario 4：PrivateLink](gateway/Scenario4PrivateLink/README.md)

## 本地验证

```bash
python3 -m unittest discover -s tests -v
python3 -m ruff check business-lambdas cloudformation gateway tests
bash -n cloudformation/deploy.sh
git diff --check
```

CloudFormation静态lint还需要安装`cfn-lint`/SAM CLI；涉及真实AWS和Quick的控制面、OAuth及Desktop验收需在目标测试环境执行。

## 安全与产物边界

- 不提交secret Value、token、本地参数、deployed state或evidence；
- 外部Secrets Manager secret默认使用AWS managed key；customer managed KMS key需要额外最小`kms:Decrypt`评审；
- 删除场景一、二、三栈不会删除三只共享业务Lambda；Scenario4专属VPC Lambda会随Scenario4栈删除；外部secret、Entra app registrations、Quick Connector和artifact bucket均不由场景栈自动删除。
