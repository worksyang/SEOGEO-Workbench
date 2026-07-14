# AIDSO 页面级字段字典

> 目的：建立 **「URL / tab / 页面状态 -> 字段」** 的可查字典。  
> 范围：只基于真实页面观察，不做接口逆向。  
> 说明：
> - 同一个字段如果出现在多个板块 / tab，会拆成多行，因为它的口径、单位、空值形态可能不同。
> - “是否带标签”指这个字段本身是不是标签容器，例如 `类型`、`推荐理由`。
> - “业务含义”统一用页面语义解释，而不是接口推断。

| 字段名 | 板块 | Tab/页面状态 | URL模式 | 数据类型 | 单位 | 空值形态 | 业务含义 | 是否带标签 |
|---|---|---|---|---|---|---|---|---|
| 关键词 | 全站找词 | 搜索词 | `/KeywordAll/searchWord?keyword=` | string | 无 | 无 | 当前被比较的关键词文本 | 否 |
| 字数 | 全站找词 | 搜索词 | `/KeywordAll/searchWord?keyword=` | int | 字 | `-` | 关键词字符长度 | 否 |
| 全站下拉词数量 | 全站找词 | 搜索词 | `/KeywordAll/searchWord?keyword=` | int/float | 个、`w` | `-` | 该词在全站聚合视角下的下拉词规模 | 否 |
| 抖·日均 | 全站找词 | 搜索词 | `/KeywordAll/searchWord?keyword=` | int/float | 次、`w` | `-` | 该词在抖音侧的日均热度指标 | 否 |
| 快·日均 | 全站找词 | 搜索词 | `/KeywordAll/searchWord?keyword=` | int/float | 次、`w` | `-` | 该词在快手侧的日均热度指标 | 否 |
| 红·日均 | 全站找词 | 搜索词 | `/KeywordAll/searchWord?keyword=` | int/float | 次、`w` | `-` | 该词在小红书侧的日均热度指标 | 否 |
| 微·月均 | 全站找词 | 搜索词 | `/KeywordAll/searchWord?keyword=` | int/float | 次、`w` | `-` | 该词在微信侧的月均热度指标 | 否 |
| 哔·搜索指数 | 全站找词 | 搜索词 | `/KeywordAll/searchWord?keyword=` | int/float | 指数、`w` | `-` | 该词在 BSO 侧的搜索指数 | 否 |
| 月覆盖人次 | DSO | 搜索词 | `/KeywordDouyin/searchWord?keyword=` | int/float | 次、`w`、`亿` | `0`、`-` | 抖音侧该词关联内容的月度覆盖规模 | 否 |
| 7日搜索人次 | DSO | 搜索词 | `/KeywordDouyin/searchWord?keyword=` | int/float/string | 次、`w`、`平均:` | 空白 | 抖音侧近 7 天搜索规模或均值提示 | 否 |
| 字数 | DSO | 搜索词 | `/KeywordDouyin/searchWord?keyword=` | int | 字 | 无 | 关键词长度 | 否 |
| 下拉词数量 | DSO | 搜索词 | `/KeywordDouyin/searchWord?keyword=` | int/float | 个、`w` | `0` | 抖音下拉词条目数量 | 否 |
| 下拉词月覆盖 | DSO | 搜索词 | `/KeywordDouyin/searchWord?keyword=` | int/float | 次、`w`、`亿` | `0` | 抖音下拉词集合的月度覆盖规模 | 否 |
| 类型 | DSO | 搜索词 | `/KeywordDouyin/searchWord?keyword=` | string[] | 无 | `-` | 词的分类标签，如蓝海词、黑马词 | 是 |
| 竞争度 | DSO | 搜索词 | `/KeywordDouyin/searchWord?keyword=` | string/empty | 无 | 空白 | 页面给出的竞争强弱信息 | 否 |
| 相关词月覆盖 | DSO | 相关词 | `/KeywordDouyin/correlationWord?keyword=` | int/float | 次、`w` | 空表/空白 | 相关词集合对应的月覆盖规模 | 否 |
| 7日搜索人次 | DSO | 相关词 | `/KeywordDouyin/correlationWord?keyword=` | int/float/string | 次、`w`、`平均:` | 空表 | 相关词近 7 天搜索规模 | 否 |
| 字数 | DSO | 相关词 | `/KeywordDouyin/correlationWord?keyword=` | int | 字 | 空表 | 相关词长度 | 否 |
| 类型 | DSO | 相关词 | `/KeywordDouyin/correlationWord?keyword=` | string[] | 无 | 空表/`-` | 相关词标签分类 | 是 |
| 竞争度 | DSO | 相关词 | `/KeywordDouyin/correlationWord?keyword=` | string/empty | 无 | 空表 | 相关词竞争信息 | 否 |
| 月覆盖人次 | DSO | 下拉词 | `/KeywordDouyin/sugWord?keyword=` | int/float | 次、`w` | `0`、`-` | 抖音下拉词自身的月覆盖规模 | 否 |
| 7日搜索人次 | DSO | 下拉词 | `/KeywordDouyin/sugWord?keyword=` | int/float/string | 次、`w`、`平均:` | 空白 | 抖音下拉词近 7 天搜索规模 | 否 |
| 字数 | DSO | 下拉词 | `/KeywordDouyin/sugWord?keyword=` | int | 字 | 无 | 下拉词长度 | 否 |
| 类型 | DSO | 下拉词 | `/KeywordDouyin/sugWord?keyword=` | string[] | 无 | `-` | 下拉词标签分类 | 是 |
| 竞争度 | DSO | 下拉词 | `/KeywordDouyin/sugWord?keyword=` | string/empty | 无 | 空白 | 下拉词竞争信息 | 否 |
| 搜索点击率 | DSO | 电商词 / 电商详情 | `/KeywordDouyin/commerce?keyword=`、`/keyWordDetail/commerce?keyword=` | percent | `%` | `0` | 搜索后发生点击的比例 | 否 |
| 订单量 | DSO | 电商词 / 电商详情 | 同上 | range/string | 档位 | `0-50` 等档位 | 搜索相关商品订单规模区间 | 否 |
| 销售指数 | DSO | 电商词 / 电商详情 | 同上 | int/float | 指数、`w` | `0` | 交易表现强度 | 否 |
| GPMS指数 | DSO | 电商词 / 电商详情 | 同上 | int | 指数 | `0` | 页面提供的电商效率指标 | 否 |
| 搜索成交率 | DSO | 电商词 / 电商详情 | 同上 | percent | `%` | `0` | 搜索后转化成成交的比例 | 否 |
| 排名 | DSO | 小蓝词榜 / 作品排名 / 话题榜 | `/KeywordDouyinHot/*`、`/keyWordDetail/competeAnalyze?keyword=` | int | 名次 | 无 | 榜单序号 | 否 |
| 视频数 | DSO | 小蓝词榜 / 话题榜 | `/KeywordDouyinHot/blue`、`/KeywordDouyinHot/topicList` | int | 个 | `0` | 相关视频内容数量 | 否 |
| 点赞总量 | DSO | 小蓝词榜 | `/KeywordDouyinHot/blue` | int/float | 次、`w` | `0` | 榜单词关联内容的累计点赞量 | 否 |
| 搜索热度 | DSO | 行业榜 | `/KeywordDouyinHot/industryHot` | int/float | 指数、`w` | `-` | 行业榜词的搜索热度 | 否 |
| 消耗指数 | DSO | 行业榜 | `/KeywordDouyinHot/industryHot` | int/float | 指数 | `-` | 行业竞价或消耗强度指标 | 否 |
| 行业 | DSO | 行业榜 | `/KeywordDouyinHot/industryHot` | string | 无 | 无 | 词条所属行业分类 | 否 |
| 话题信息 | DSO | 话题榜 | `/KeywordDouyinHot/topicList` | string | 无 | `暂无数据` | 榜单话题文本 | 否 |
| 热度 | DSO | 话题榜 | `/KeywordDouyinHot/topicList` | int/float | 指数、`w` | `暂无数据` | 话题热度值 | 否 |
| 播放量 | DSO | 话题榜 | `/KeywordDouyinHot/topicList` | int/float | 次、`w` | `暂无数据` | 话题总播放量 | 否 |
| 平均播放量 | DSO | 话题榜 | `/KeywordDouyinHot/topicList` | int/float | 次、`w` | `暂无数据` | 单内容平均播放水平 | 否 |
| 月均搜索量 | RSO | 搜索词 | `/KeywordXhs/searchWord?keyword=` | int/float | 次、`w` | `-` | 小红书该词月均搜索规模 | 否 |
| 7日搜索人次 | RSO | 搜索词 | `/KeywordXhs/searchWord?keyword=` | int/float/string | 次、`w`、`平均:` | 空白 | 小红书近 7 天搜索规模 | 否 |
| 下拉词数量 | RSO | 搜索词 | `/KeywordXhs/searchWord?keyword=` | int/float | 个、`w` | `0` | 小红书下拉词规模 | 否 |
| 竞争程度 | RSO | 搜索词 | `/KeywordXhs/searchWord?keyword=` | string | 高/中/低 | `-` | 小红书该词竞争强弱 | 否 |
| 推荐理由 | RSO | 搜索词 | `/KeywordXhs/searchWord?keyword=` | string[] | 无 | `-` | 词的推荐标签集合 | 是 |
| 市场出价 | RSO | 搜索词 | `/KeywordXhs/searchWord?keyword=` | decimal | 元 | `-` | 小红书该词的商业投放出价信号 | 否 |
| 月均搜索量 | RSO | 相关词 | `/KeywordXhs/correlationWord?keyword=` | int/float | 次、`w` | `-` | RSO 相关词月均搜索规模 | 否 |
| 7日搜索人次 | RSO | 相关词 | `/KeywordXhs/correlationWord?keyword=` | int/float/string | 次、`w`、`平均:` | 空白 | RSO 相关词近 7 天搜索规模 | 否 |
| 字数 | RSO | 相关词 | `/KeywordXhs/correlationWord?keyword=` | int | 字 | 无 | RSO 相关词长度 | 否 |
| 竞争程度 | RSO | 相关词 | `/KeywordXhs/correlationWord?keyword=` | string | 高/中/低 | `-` | RSO 相关词竞争强弱 | 否 |
| 推荐理由 | RSO | 相关词 | `/KeywordXhs/correlationWord?keyword=` | string[] | 无 | `-` | RSO 相关词标签集合 | 是 |
| 市场出价 | RSO | 相关词 | `/KeywordXhs/correlationWord?keyword=` | decimal | 元 | `-` | RSO 相关词商业出价 | 否 |
| 月均搜索量 | RSO | 下拉词 | `/KeywordXhs/sugWord?keyword=` | int/float | 次、`w` | `-` | RSO 下拉词月均搜索规模 | 否 |
| 竞争程度 | RSO | 下拉词 | `/KeywordXhs/sugWord?keyword=` | string | 高/中/低 | `-` | RSO 下拉词竞争强弱 | 否 |
| 推荐理由 | RSO | 下拉词 | `/KeywordXhs/sugWord?keyword=` | string[] | 无 | `-` | RSO 下拉词标签集合 | 是 |
| 市场出价 | RSO | 下拉词 | `/KeywordXhs/sugWord?keyword=` | decimal | 元 | `-` | RSO 下拉词商业出价 | 否 |
| 近7天搜索量 | KSO | 搜索词 | `/KeywordKSO/searchWord?keyword=` | int/float | 次、`w` | `-` | 快手该词近 7 天搜索规模 | 否 |
| 7日搜索人次 | KSO | 搜索词 | `/KeywordKSO/searchWord?keyword=` | int/float/string | 次、`w`、`平均:` | 空白 | 快手近 7 天搜索人数或均值 | 否 |
| 下拉词数量 | KSO | 搜索词 | `/KeywordKSO/searchWord?keyword=` | int/float | 个、`w` | `0` | 快手下拉词规模 | 否 |
| 下拉词近7天搜索量 | KSO | 搜索词 | `/KeywordKSO/searchWord?keyword=` | int/float | 次、`w` | `0` | 快手下拉词集合近 7 天搜索规模 | 否 |
| 竞争程度 | KSO | 搜索词 | `/KeywordKSO/searchWord?keyword=` | string/empty | 无 | 空白 | 快手该词竞争信息 | 否 |
| 类型 | KSO | 搜索词 | `/KeywordKSO/searchWord?keyword=` | string[] | 无 | `-` | 快手词标签，如低成本词、高点击词 | 是 |
| 近7天搜索量 | KSO | 相关词 | `/KeywordKSO/correlationWord?keyword=` | int/float | 次、`w` | `-` | 快手相关词近 7 天搜索规模 | 否 |
| 竞争程度 | KSO | 相关词 | `/KeywordKSO/correlationWord?keyword=` | string/empty | 无 | 空白 | 快手相关词竞争信息 | 否 |
| 类型 | KSO | 相关词 | `/KeywordKSO/correlationWord?keyword=` | string[] | 无 | `-` | 快手相关词标签集合 | 是 |
| 近7天搜索量 | KSO | 下拉词 | `/KeywordKSO/sugWord?keyword=` | int/float | 次、`w` | `-` | 快手下拉词近 7 天搜索规模 | 否 |
| 类型 | KSO | 下拉词 | `/KeywordKSO/sugWord?keyword=` | string[] | 无 | `-` | 快手下拉词标签集合 | 是 |
| 热度 | KSO | 搜索飙升榜 | `/KeywordKSOList/hotWord` 或榜单页 | int/float | `w` | 无 | 榜单热度数值 | 否 |
| 标签状态 | KSO | 搜索飙升榜 | 同上 | string[] | 无 | 无 | 榜单额外状态，如新、热、直播 | 是 |
| 月均搜索量 | WSO | 搜索词 | `/KeywordWSO/searchWord?keyword=` | int/float | 次、`w` | `-` | 微信该词月均搜索规模 | 否 |
| 月均点击量 | WSO | 搜索词 | `/KeywordWSO/searchWord?keyword=` | int/float | 次、`w` | `0`、`-` | 微信该词月均点击规模 | 否 |
| 下拉词数量 | WSO | 搜索词 | `/KeywordWSO/searchWord?keyword=` | int/float | 个、`w` | `0` | 微信下拉词规模 | 否 |
| 竞争程度 | WSO | 搜索词 | `/KeywordWSO/searchWord?keyword=` | string/empty | 无 | 空白 | 微信该词竞争程度 | 否 |
| 推荐理由 | WSO | 搜索词 | `/KeywordWSO/searchWord?keyword=` | string[] | 无 | `-` | 微信词标签集合 | 是 |
| 月均搜索量 | WSO | 相关词 | `/KeywordWSO/correlationWord?keyword=` | int/float | 次、`w` | `-` | 微信相关词月均搜索规模 | 否 |
| 月均点击量 | WSO | 相关词 | `/KeywordWSO/correlationWord?keyword=` | int/float | 次、`w` | `0`、`-` | 微信相关词月均点击规模 | 否 |
| 推荐理由 | WSO | 相关词 | `/KeywordWSO/correlationWord?keyword=` | string[] | 无 | `-` | 微信相关词标签集合 | 是 |
| 月均搜索量 | WSO | 下拉词 | `/KeywordWSO/sugWord?keyword=` | int/float | 次、`w` | `-` | 微信下拉词月均搜索规模 | 否 |
| 月均点击量 | WSO | 下拉词 | `/KeywordWSO/sugWord?keyword=` | int/float | 次、`w` | `0`、`-` | 微信下拉词月均点击规模 | 否 |
| 推荐理由 | WSO | 下拉词 | `/KeywordWSO/sugWord?keyword=` | string[] | 无 | `-` | 微信下拉词标签集合 | 是 |
| 模糊搜索 | WSO | 批量模式 | `/KeywordWSO/searchWord?keyword=` + 批量 | string | 无 | 无 | 批量模式的输入方式说明 | 否 |
| 个人版最多输入5个词 | WSO | 批量模式 | 同上 | limit/string | 个 | 无 | 个人版批量输入上限 | 否 |
| 文件上传 | WSO | 批量模式 | 同上 | boolean/UI | 无 | 无 | 批量导入入口 | 否 |
| 搜索指数 | BSO | 搜索词 | `/KeywordBSO/searchWord?keyword=` | int/float | 指数、`w` | `-` | BSO 平台该词搜索指数 | 否 |
| 下拉词数量 | BSO | 搜索词 | `/KeywordBSO/searchWord?keyword=` | int/float | 个、`w` | `0` | BSO 下拉词规模 | 否 |
| 下拉词月搜索 | BSO | 搜索词 | `/KeywordBSO/searchWord?keyword=` | int/float | 次、`w` | `0` | BSO 下拉词集合月搜索规模 | 否 |
| 竞争程度 | BSO | 搜索词 | `/KeywordBSO/searchWord?keyword=` | string | 低/中/高 | `-` | BSO 竞争强弱 | 否 |
| 类型 | BSO | 搜索词 | `/KeywordBSO/searchWord?keyword=` | string[] | 无 | `-` | BSO 词标签，如热搜词、高转化词 | 是 |
| 搜索指数 | BSO | 相关词 | `/KeywordBSO/correlationWord?keyword=` | int/float | 指数、`w` | `-` | BSO 相关词搜索指数 | 否 |
| 字数 | BSO | 相关词 | `/KeywordBSO/correlationWord?keyword=` | int | 字 | 无 | BSO 相关词长度 | 否 |
| 竞争程度 | BSO | 相关词 | `/KeywordBSO/correlationWord?keyword=` | string | 低/中/高 | `-` | BSO 相关词竞争强弱 | 否 |
| 类型 | BSO | 相关词 | `/KeywordBSO/correlationWord?keyword=` | string[] | 无 | `-` | BSO 相关词标签集合 | 是 |
| 搜索指数 | BSO | 下拉词 | `/KeywordBSO/sugWord?keyword=` | int/float | 指数、`w` | `-` | BSO 下拉词搜索指数 | 否 |
| 类型 | BSO | 下拉词 | `/KeywordBSO/sugWord?keyword=` | string[] | 无 | `-` | BSO 下拉词标签集合 | 是 |
| 内容指数 | BSO | 详情页 | `/BsoKeyWordDetail/detail?keyword=` | float | 指数 | `0-1` 图表 | 与内容表现相关的趋势指标 | 否 |
| 新增稿件量指数 | BSO | 详情页 | 同上 | float | 指数 | `0-1` 图表 | 新增稿件增长强度 | 否 |
| 新增相关UP主指数 | BSO | 详情页 | 同上 | float | 指数 | `0-1` 图表 | 新增相关创作者增长强度 | 否 |
| 播放量指数 | BSO | 详情页 | 同上 | float | 指数 | `0-1` 图表 | 播放表现趋势强度 | 否 |
| 互动量指数 | BSO | 详情页 | 同上 | float | 指数 | `0-1` 图表 | 互动表现趋势强度 | 否 |
| 搜索量指数 | BSO | 详情页 | 同上 | float | 指数 | `0-1` 图表 | 搜索表现趋势强度 | 否 |
| 官方保护 | BSO | 详情页 | 同上 | boolean/UI | 无 | 可能无数据 | 官方保护开关或标识 | 否 |
| 搜索指数 | TSO | 搜索词 | `/KeywordTSO/searchWord?keyword=` | int/float | 指数、`w` | `无官方数据` | 海外/TikTok 侧搜索热度 | 否 |
| 字数 | TSO | 搜索词 | `/KeywordTSO/searchWord?keyword=` | int | 字 | 无 | 关键词长度 | 否 |
| 下拉词数量 | TSO | 搜索词 | `/KeywordTSO/searchWord?keyword=` | int | 个 | `0` | TSO 下拉词规模 | 否 |
| 下拉词月搜索 | TSO | 搜索词 | `/KeywordTSO/searchWord?keyword=` | int/float | 次、`w` | `0` | TSO 下拉词集合月搜索规模 | 否 |
| 国家 | TSO | 搜索页 / 详情页 | `/KeywordTSO/*`、`/TsoKeyWordDetail/detail?keyword=` | string | 国家 | 未选择 | 结果集所属国家维度 | 否 |
| 搜索指数 | TSO | 相关词 | `/KeywordTSO/correlationWord?keyword=` | int/float | 指数、`w` | `无官方数据` | TSO 相关词搜索指数 | 否 |
| 字数 | TSO | 相关词 | `/KeywordTSO/correlationWord?keyword=` | int | 字 | 无 | TSO 相关词长度 | 否 |
| 搜索指数 | TSO | 下拉词 | `/KeywordTSO/sugWord?keyword=` | int/float | 指数、`w` | `无官方数据` | TSO 下拉词搜索指数 | 否 |
| 话题信息 | TSO | 创意灵感话题榜 | `/KeywordTSOList/creativeIdeaTopics` | string | 无 | 无 | 海外灵感话题文本 | 否 |
| 作品数 | TSO | 创意灵感话题榜 | 同上 | int/float | 个、`w` | 无 | 话题关联作品规模 | 否 |
| 热度趋势 | TSO | 创意灵感话题榜 | 同上 | chart/UI | 无 | 无 | 话题热度变化趋势 | 否 |
| 排序 | TSO | 创意灵感话题榜 | 同上 | int | 名次 | 无 | 榜单排序位置 | 否 |
| 新上榜 | TSO | 创意灵感话题榜 | 同上 | boolean/tag | 无 | 无 | 话题是否首次进入榜单 | 是 |

