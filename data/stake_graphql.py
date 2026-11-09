"""Stake 体育查询语句。

真实端点是 ``/_api/v1/graphql``（Cosmo Router，Introspection 已关闭），
下面的查询是对着线上响应反推出来的，字段与 ``data/stake_v1.py`` 的解析一一对应。

如果哪天字段又变了，把浏览器 F12 里抓到的 ``query`` 存成
``queries/events.graphql`` 即可覆盖，不需要改代码。变量约定：

* ``$sportId: ID!``  足球是 UUID，不能用 slug（见 ``STAKE_SPORT_ID``）
* ``$limit: Int``    返回条数，实测 50 可用
* ``$type: SportSearchEnum``  可选值：``upcoming`` / ``live`` / ``all``
"""

from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
QUERY_DIR = BASE_DIR / "queries"

EVENTS_QUERY = """
query SportFixtures($sportId: String!, $limit: Int) {
  sport(sportId: $sportId) {
    id
    slug
    fixtureCount
    upcoming: fixtureList(limit: $limit, type: upcoming) {
      ...FixtureFields
    }
    live: fixtureList(limit: $limit, type: live) {
      ...FixtureFields
    }
  }
}

fragment FixtureFields on SportFixture {
  id
  stakeFixtureId
  name
  status
  marketCount
  data {
    __typename
    ... on SportFixtureDataMatch {
      startTime
      isOutright
      teams { name qualifier }
    }
  }
  tournament { id name slug category { name slug } }
  eventStatus {
    __typename
    ... on SportFixtureEventStatusData {
      homeScore
      awayScore
      matchStatus
    }
  }
  groups(limit: 6) {
    name
    templates {
      extId
      name
      markets {
        extId
        name
        specifiers
        outcomes { name odds active }
      }
    }
  }
}
"""

INTROSPECTION_QUERY = """
query IntrospectSports {
  __schema {
    queryType {
      fields { name args { name } }
    }
  }
}
"""


def load_query(name: str, default: str) -> str:
    """优先读取 queries/<name>.graphql 里的抓包查询。"""
    path = QUERY_DIR / f"{name}.graphql"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return default
    return text or default
