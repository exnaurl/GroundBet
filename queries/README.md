# 抓包覆盖查询

Stake 的体育 GraphQL schema 没有公开文档，且 **Introspection 已关闭**，默认查询写在
`data/stake_graphql.py`（端点是 `/_api/v1/graphql`）。如果线上返回 `Cannot query field`，
用浏览器抓到的真实查询覆盖：

1. 打开 stake.com 体育页，F12 → Network，筛选 `_api/v1/graphql`；
2. 复制 Request Payload 里的 `query` 字符串；
3. 存成 `queries/events.graphql`；
4. 重新运行 `python cli.py fetch`，程序会自动优先使用这个文件。

注意：

- 变量名必须是 `$sportId`（String!）和 `$limit`（Int），程序只会传这两个；
- Cosmo Router 会拒绝「声明了但没用到」的变量，所以不要保留多余的 `$type` 之类；
- 字段名可以和默认查询不同，但解析器 `data/stake_v1.py` 认的是
  `sport.fixtureList[].data/eventStatus/groups` 这套结构，改动太大时需要同步调整解析。
