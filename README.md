# 指标口径与分析实验平台

面向业务指标定义、口径管理、聚合查询、分群分析、A/B实验设计、分析方案管理与数据服务发布的数据分析工程平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   ├── app/services/impact.py 口径影响分析与验证执行
│   ├── app/impact_keys.py    影响键与指纹计算
│   ├── app/acceptance.py     换版验收脚本
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 指标口径 | `metric` | 指标定义 | 指标编号、指标名称、计算公式 |
| 聚合查询 | `queryboard` | 查询模板 | 模板编号、指标名称、维度字段 |
| 分群分析 | `segment` | 人群分群 | 分群编号、分群名称、筛选规则 |
| 实验设计 | `abtest` | 实验方案 | 方案编号、实验名称、对照组定义 |
| 分析方案 | `analysis` | 分析方案 | 方案编号、分析主题、数据集范围 |
| 看板配置 | `dashboard` | 看板页面 | 看板编号、看板名称、包含指标 |
| 数据服务 | `delivery` | 服务版本 | 服务编号、数据集名称、查询参数定义 |
| 漏斗分析 | `funnel` | 漏斗定义 | 漏斗编号、漏斗名称、步骤序列 |
| 同期群分析 | `cohort` | 同期群定义 | 群组编号、群组名称、划分事件 |
| 归因模型 | `attribution` | 归因规则 | 规则编号、归因类型、回溯窗口天 |
| 指标预警 | `alertconfig` | 预警规则 | 预警编号、关联指标、比较运算符 |
| 报表调度 | `reportsch` | 报表任务 | 任务编号、报表名称、包含指标 |
| 维度属性 | `dimprop` | 维度属性 | 属性编号、维度名称、属性名称 |
| 多维下钻 | `datadrill` | 下钻路径 | 路径编号、起始维度、下钻层级 |
| 预测模型 | `forecast` | 预测方案 | 方案编号、预测目标、算法名称 |
| 标签管理 | `tagmgr` | 标签定义 | 标签编号、标签名称、标签分类 |
| 洞察推送 | `insight` | 洞察报告 | 报告编号、报告标题、分析主题 |
| 数据验证 | `dataval` | 验证任务 | 任务编号、验证数据集、验证类型 |
| 口径影响分析 | `impact` | 影响执行单 | impact_run、四个集合、样本结论、快照 |

## 指标换版到验证执行的调用链

```text
① 版本发布（metric / metric_revision）
   指标新增版本：formula_hash、aggregation_grain、compare_axis、tolerance、baseline_uri
        │  只改一个字段也登记为新 revision
        ▼
② 影响分析（ImpactService.analyze，位于写验证队列之前）
   归一化粒度 → grain_hash；基准内容 → baseline_digest
   impact_key = (metric_id, grain_hash, baseline_uri, tolerance)
   逐个 DATA-* 任务用“任务绑定视角”重算基线指纹：
     指纹变化 / tolerance 变化          → affected（生成成员，等待重验）
     指纹不变且已有完整结论             → reused（沿用历史结论）
     指纹不变但没有完整结论             → pending
   重复分析同一 impact_key + revision   → 直接返回已有 run，不重复登记
        │
        ▼
③ 结论缓存（metric_conclusion）
   只有完整落库（complete / abnormal）的结论可复用、可标已验证
   每条结论绑定 task_code + metric_revision + impact_key + baseline_digest
   reused 任务不生成新结论、不追加快照条目
        │
        ▼
④ 验证执行（ImpactService.execute）
   先追加 building 快照，原 active 快照在新结论完整前保持可读
   只跑 affected 成员；成员失败被隔离，下次触发从该成员的样本缺口继续
   同一 metric 的执行持锁串行，重复触发同一 run 直接返回，不重复处理
   样本级结论：02-29 闰日=通过、跨月首日=异常、空地区=待补数（gap）
        │
        ▼
⑤ 快照提交
   所有 affected 成员终态后才把 building 切成 active；
   旧 active 转 superseded 保留历史，不删除、不提前清空
        │
        ▼
⑥ 结果查询（/api/impact/runs/{run_id}）
   输出 affected / reused / pending / abnormal 四个集合，
   每条结论带 metric_revision，未命中的任务不产生新记录
```

### 影响键与判定规则

- `grain_hash`：`aggregation_grain` 归一化后的指纹，字段顺序保留、空白与空字段剔除。
- `baseline_digest`：由 `baseline_uri + formula_hash + grain_hash + compare_axis` 计算，
  基准地址不变但内容变化时 digest 也会变。
- 任务只有绑定在本指标对应字段上才会命中；例如只改订单量的聚合粒度时，
  绑定自有基准和 `user_id` 粒度的退款率任务继续复用 r2 结论。
- `tolerance` 不进入 `baseline_digest`，但会改变通过/异常边界，因此单独判断。

### 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/metric/{id}/impact?metric_revision=r3` | 查看指定版本的影响集合 |
| POST | `/api/metric/{id}/revisions` | 发布新版本并立即做影响分析 |
| GET | `/api/impact/revisions/{metric_id}` | 列出版本与影响键字段 |
| POST | `/api/impact/analyze` | 对已发布版本做影响分析（同一 run 幂等） |
| GET | `/api/impact/runs?metric_id=` | 影响执行单列表 |
| POST | `/api/impact/runs/{run_id}/execute` | 执行 affected 成员；重复触发只续跑未完成项 |
| POST | `/api/impact/runs/{run_id}/backfill` | 登记样本补数，下一次执行从缺口继续 |
| GET | `/api/impact/runs/{run_id}/snapshots` | 查看 building / active / superseded 快照 |
| GET | `/api/impact/trace?metric_id=` | 版本 → run → 结论 → 快照的完整链路 |

验收脚本（不依赖额外测试库）：

```bash
cd backend && .venv/bin/python -m app.acceptance
```

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据分析与数据应用
