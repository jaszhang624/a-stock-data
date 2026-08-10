# Upstream Function Inventory

Generated from SKILL.md code blocks. Total: 48 blocks, ~60 functions.

## Helper Functions (upstream/common.py)
| Function | Source | Description |
|----------|--------|-------------|
| `tdx_client` | mootdx | 通达信客户端工厂函数 |
| `_probe` | mootdx | TCP probe helper |
| `_validate` | mootdx | 连接验证 helper |
| `get_prefix` | local | 股票代码前缀路由 (sh/sz/bj) |
| `em_get` | eastmoney | 东财 HTTP 请求封装 (含限流) |
| `eastmoney_datacenter` | eastmoney | 东财数据中心通用接口 |

## Market Data (upstream/tdx.py, upstream/tencent.py)
| Function | Source | Description |
|----------|--------|-------------|
| `tencent_quote` | tencent | 实时行情 (腾讯) |
| `baidu_kline_with_ma` | baidu | K线 + MA均线 (百度) |
| `full_valuation` | tencent+eastmoney | 综合估值分析 |

## Eastmoney (upstream/eastmoney.py)
| Function | Source | Description |
|----------|--------|-------------|
| `eastmoney_concept_blocks` | eastmoney | 板块归属 |
| `eastmoney_fund_flow_minute` | eastmoney | 分钟级资金流 |
| `board_fund_flow` | eastmoney | 板块资金流 |
| `_page` | eastmoney | 分页 helper |
| `eastmoney_reports` | eastmoney | 研报列表 |
| `download_pdf` | eastmoney | PDF下载 |
| `eastmoney_industry_reports` | eastmoney | 行业研报 |
| `industry_comparison` | eastmoney | 行业对比 |
| `stock_fund_flow_120d` | eastmoney | 120日资金流 |
| `eastmoney_stock_news` | eastmoney | 个股新闻 |
| `eastmoney_global_news` | eastmoney | 全球新闻 |
| `eastmoney_stock_info` | eastmoney | F10信息 |
| `em_zt_pool` | eastmoney | 涨停池 |
| `em_zb_pool` | eastmoney | 跌停池 |
| `em_dt_pool` | eastmoney | 地天板池 |
| `em_yzt_pool` | eastmoney | Yesterday涨停池 |
| `_fmt_zt_time` | eastmoney | 时间格式化 helper |
| `_em_zt_api` | eastmoney | 涨停API封装 |
| `em_hot_rank` | eastmoney | 热度排行 |
| `em_hot_concept` | eastmoney | 概念热度 |

## Tonghuashun (upstream/ths.py)
| Function | Source | Description |
|----------|--------|-------------|
| `ths_eps_forecast` | ths | 一致预期/盈利预测 |
| `ths_hot_reason` | ths | 热点原因 |
| `ths_limit_up_pool` | ths | 涨停题材归因 |
| `ths_hot_list` | ths | 热点列表 |

## Sina (upstream/sina.py)
| Function | Source | Description |
|----------|--------|-------------|
| `sina_financial_report` | sina | 财务报表 (利润/现金流/资产负债) |
| `sina_option_codes` | sina | 期权合约列表 |
| `sina_option_tquote` | sina | 期权T型报价 |
| `sina_option_greeks` | sina | 期权希腊字母 |
| `_opt_f` | sina | 格式化 helper |
| `_sina_opt_list` | sina | 期权列表API |

## CNInfo (upstream/cninfo.py)
| Function | Source | Description |
|----------|--------|-------------|
| `cninfo_announcements` | cninfo | 巨潮公告 |
| `_cninfo_ts_to_date` | cninfo | 时间戳转换 helper |
| `_cninfo_orgid` | cninfo | OrgID查询 helper |
| `cninfo_irm` | cninfo | 投资者关系互动平台 |

## CLS (upstream/cls.py)
| Function | Source | Description |
|----------|--------|-------------|
| `cls_telegraph` | cls | 财联社电报 |

## iwencai (upstream/other.py)
| Function | Source | Description | Requires Key |
|----------|--------|-------------|----------|
| `iwencai_search` | iwencai | 语义搜索研报 | Yes |
| `iwencai_query` | iwencai | NL查询 | Yes |
| `_claw_headers` | iwencai | 鉴权 headers helper | Yes |
| `dedup_articles` | iwencai | 文章去重 | No |

## Signals & Analysis (upstream/other.py)
| Function | Source | Description |
|----------|--------|-------------|
| `dragon_tiger_board` | local | 龙虎榜 |
| `daily_dragon_tiger` | local | 每日龙虎榜筛选 |
| `lockup_expiry` | local | 解禁日期 |
| `margin_trading` | local | 融资融券 |
| `block_trade` | local | 大宗交易 |
| `holder_num_change` | local | 股东户数变化 |
| `dividend_history` | local | 分红历史 |
| `limit_up_sentiment` | local | 涨停情绪 |
| `hsgt_realtime` | local | 沪深港通实时 |
| `_northbound_cache_path` | local | 缓存路径 helper |
| `_save_northbound_snapshot` | local | 快照保存 |
| `_load_northbound_history` | local | 历史加载 |

## Valuation Helpers (upstream/common.py)
| Function | Source | Description |
|----------|--------|-------------|
| `forward_pe` | local | 前向PE计算 |
| `pe_digestion` | local | PE消化时间 |
| `calc_peg` | local | PEG计算 |

## Backup Functions (upstream/other.py)
| Function | Source | Description |
|----------|--------|-------------|
| `dragon_tiger_backup` | backup | 龙虎榜备用源 |
| `fund_flow_backup` | backup | 资金流备用源 |
| `announcements_backup` | backup | 公告备用源 |

## Global State (needs thread safety)
| Variable | Module | Purpose |
|----------|--------|---------|
| `EM_SESSION` | eastmoney.py | requests.Session (东财) |
| `EM_MIN_INTERVAL` | eastmoney.py | 最小请求间隔 (秒) |
| `_em_last_call` | eastmoney.py | 上次调用时间戳 |
| `_TDX_SERVERS` | tdx.py | 通达信服务器列表 |
