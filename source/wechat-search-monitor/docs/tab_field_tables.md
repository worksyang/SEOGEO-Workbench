# AIDSO 各板块 Tab 完整字段表

> 目的：把 **每个平台 / 每个 tab / 每个页面状态** 的完整字段列表单独固定下来。  
> 说明：只记录真实页面上能看到的列与核心指标，不做接口推断。

## 1. 全站找词

| Tab/状态 | URL | 字段 |
|---|---|---|
| 搜索词 | `/KeywordAll/searchWord?keyword=` | `关键词 / 字数 / 全站下拉词数量 / 抖·日均 / 快·日均 / 红·日均 / 微·月均 / 哔·搜索指数` |

## 2. DSO

| Tab/状态 | URL | 字段 |
|---|---|---|
| 搜索词 | `/KeywordDouyin/searchWord?keyword=` | `月覆盖人次 / 7日搜索人次 / 字数 / 下拉词数量 / 下拉词月覆盖 / 类型 / 竞争度` |
| 相关词 | `/KeywordDouyin/correlationWord?keyword=` | `7日搜索人次 / 字数 / 相关词月覆盖 / 类型 / 竞争度` |
| 下拉词 | `/KeywordDouyin/sugWord?keyword=` | `月覆盖人次 / 7日搜索人次 / 字数 / 类型 / 竞争度` |
| 行业词 | `/KeywordDouyin/industryWord?keyword=` | 当前个人版被 `权限不足` 拦截，主表字段不可见 |
| 电商词 | `/KeywordDouyin/commerce?keyword=` | `月覆盖人次 / 7日搜索人次 / 搜索点击率 / 订单量 / 销售指数 / GPMS指数 / 搜索成交率` |
| 小蓝词榜 | `/KeywordDouyinHot/blue` | `排名 / 关键词 / 月覆盖人次 / 7日搜索人次 / 竞争度 / 视频数 / 点赞总量 / 搜索点击率 / 搜索成交率 / 操作` |
| 电商词热搜榜 | `/KeywordDouyinHot/commerceHot` | `关键词 / 月覆盖人次 / 7日搜索人次 / 搜索点击率 / 订单量 / 销售指数 / GPMS指数 / 搜索成交率 / 操作` |
| 电商词飙升榜 | `/KeywordDouyinHot/commerceLast` | 同 `电商词热搜榜` |
| 行业词热搜榜 | `/KeywordDouyinHot/industryHot` | `关键词 / 搜索热度 / 消耗指数 / 月覆盖人次 / 7日搜索人次 / 字数 / 竞争度 / 行业 / 操作` |
| 话题总榜 | `/KeywordDouyinHot/topicList` | `话题信息 / 热度趋势 / 热度 / 视频数 / 播放量 / 平均播放量` |
| 话题飙升榜 | `/KeywordDouyinHot/topicSurge` | 同 `话题总榜`，当前样本为 `暂无数据` |

## 3. RSO

| Tab/状态 | URL | 字段 |
|---|---|---|
| 搜索词 | `/KeywordXhs/searchWord?keyword=` | `月均搜索量 / 7日搜索人次 / 字数 / 下拉词数量 / 竞争程度 / 推荐理由 / 市场出价` |
| 相关词 | `/KeywordXhs/correlationWord?keyword=` | `月均搜索量 / 7日搜索人次 / 字数 / 竞争程度 / 推荐理由 / 市场出价` |
| 下拉词 | `/KeywordXhs/sugWord?keyword=` | `月均搜索量 / 7日搜索人次 / 字数 / 竞争程度 / 推荐理由 / 市场出价` |
| 行业词 | `/KeywordXhs/industryWord?keyword=` | 当前个人版被 `权限不足` 拦截，主表字段不可见 |

## 4. KSO

| Tab/状态 | URL | 字段 |
|---|---|---|
| 搜索词 | `/KeywordKSO/searchWord?keyword=` | `近7天搜索量 / 7日搜索人次 / 字数 / 下拉词数量 / 下拉词近7天搜索量 / 竞争程度 / 类型` |
| 相关词 | `/KeywordKSO/correlationWord?keyword=` | `近7天搜索量 / 7日搜索人次 / 字数 / 竞争程度 / 类型` |
| 下拉词 | `/KeywordKSO/sugWord?keyword=` | `近7天搜索量 / 7日搜索人次 / 字数 / 竞争程度 / 类型` |
| 行业词 | `/KeywordKSO/industryWord?keyword=` | 当前个人版被权限文案截断，主表字段未完整确认 |
| 搜索飙升榜 | 独立榜单页 | `排名 / 关键词 / 热度` |

## 5. WSO

| Tab/状态 | URL | 字段 |
|---|---|---|
| 搜索词 | `/KeywordWSO/searchWord?keyword=` | `月均搜索量 / 月均点击量 / 下拉词数量 / 字数 / 竞争程度 / 推荐理由` |
| 相关词 | `/KeywordWSO/correlationWord?keyword=` | `月均搜索量 / 月均点击量 / 字数 / 竞争程度 / 推荐理由` |
| 下拉词 | `/KeywordWSO/sugWord?keyword=` | `月均搜索量 / 月均点击量 / 字数 / 竞争程度 / 推荐理由` |
| 行业词 | `/KeywordWSO/industryWord?keyword=` | 当前个人版被 `权限不足` 拦截，主表字段不可见 |
| 批量模式 | `/KeywordWSO/searchWord?keyword=` + `批量` | `月均搜索量 / 月均点击量 / 下拉词数量 / 字数 / 竞争程度 / 推荐理由` |

## 6. BSO

| Tab/状态 | URL | 字段 |
|---|---|---|
| 搜索词 | `/KeywordBSO/searchWord?keyword=` | `搜索指数 / 字数 / 下拉词数量 / 下拉词月搜索 / 竞争程度 / 类型` |
| 相关词 | `/KeywordBSO/correlationWord?keyword=` | `搜索指数 / 字数 / 竞争程度 / 类型` |
| 下拉词 | `/KeywordBSO/sugWord?keyword=` | `搜索指数 / 字数 / 竞争程度 / 类型` |

## 7. TSO

| Tab/状态 | URL | 字段 |
|---|---|---|
| 搜索词 | `/KeywordTSO/searchWord?keyword=` | `搜索指数 / 字数 / 下拉词数量 / 下拉词月搜索` |
| 相关词 | `/KeywordTSO/correlationWord?keyword=` | `搜索指数 / 字数` |
| 下拉词 | `/KeywordTSO/sugWord?keyword=` | `搜索指数 / 字数` |
| 创意灵感话题榜 | `/KeywordTSOList/creativeIdeaTopics` | `排序 / 话题信息 / 作品数 / 热度趋势 / 操作` |

## 8. 结构对照结论

1. `相关词` 与 `下拉词` 最接近的平台：`RSO / KSO / WSO / BSO / TSO`
2. `相关词` 与 `下拉词` 差异最大的板块：`DSO`
3. 唯一有 `市场出价` 的板块：`RSO`
4. 唯一稳定带 `月均点击量` 的板块：`WSO`
5. 唯一稳定带 `搜索指数` 且无行业词 tab 的板块：`BSO`
6. 唯一有国家维度的板块：`TSO`
7. 唯一有完整电商词 tab 和多榜单体系的板块：`DSO`

