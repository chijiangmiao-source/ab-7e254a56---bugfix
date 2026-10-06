# 轨道载荷 A/B 双槽镜像升级 — 断电安全验收台

模拟轨道载荷维护员的启动镜像升级流程。核心安全保证：

- **任意时点断电都不会引导摘要不符或未确认的候选**；
- **新版本生效后永不回退**（旧槽位标记 `SUPERSEDED`，恢复时永不选择）；
- 恢复时**仅从「清单完整且已确认」的槽位中选定唯一活动槽位**，并展示逐槽诊断证据与裁决理由；
- 两个页面并发提交不同候选时，**仅一个请求取得当前代次的升级资格**，另一个得到稳定 `409` 且不改写活动版本。

## 架构

```
backend/          FastAPI 服务
  models.py       槽位/设备/恢复报告领域模型（EMPTY→CANDIDATE→VERIFIED→CONFIRMED / REJECTED / SUPERSEDED）
  versioning.py   点分数字版本比较（候选必须严格更高）
  store.py        SQLite(WAL) 持久化：槽位清单、候选阶段、确认代次、资格令牌、诊断证据、镜像 BLOB
  service.py      升级编排：代次资格、摘要校验、原子确认切换、断电恢复裁决
  api.py          HTTP API + 托管 web/dist 静态页面
web/              Vite 原生 JS 前端（中文界面，全部操作经真实 API）
tests/            pytest（14 个用例：三种断电、损坏候选、并发裁决、防回退、重开一致）
scripts/
  verify.sh       一次性验收：pytest → 构建页面 → 真实 uvicorn → HTTP 冒烟
  smoke_http.py   断电恢复与并发裁决的 HTTP 冒烟（63 条断言）
Dockerfile        运行镜像（多阶段：Node 构建页面 + Python 运行）
Dockerfile.verify 验收镜像（含 Node/Python，compose 中的 verify 服务）
docker-compose.yml
```

### 安全机制要点

| 故障点 | 断电时持久化状态 | 重新上电的裁决 |
| --- | --- | --- |
| 候选写入 `candidate_write` | 只落盘部分字节，槽位 `CANDIDATE`，`written < size` | 诊断 `incomplete_write`，继续引导旧槽 |
| 摘要校验 `digest_check` | 字节写完但校验结论未提交，`actual_digest` 为空 | 诊断 `unverified_candidate`，不升级 |
| 确认切换 `confirm_switch` | 候选仍 `VERIFIED`（未确认），代次不变 | 诊断 `unconfirmed_candidate`，引导旧版本；恢复后仍可再确认 |
| 镜像损坏 | 清单摘要 ≠ 实测摘要，槽位 `REJECTED`，证据保留 | 诊断 `digest_mismatch`，永不引导 |

所有变更在 SQLite `BEGIN IMMEDIATE` 事务内完成，`COMMIT` 是唯一原子切换点；并发提交由数据库写锁串行化后再做代次资格裁决，因此冲突结果稳定。

## 快速开始（Docker Compose）

```bash
# 宿主机端口可配置（默认 8080）
HOST_PORT=9090 docker compose up -d --build
curl http://localhost:9090/api/health      # 健康响应
# 浏览器打开 http://localhost:9090
```

数据保存在命名卷 `upgrade-data`（容器内 `/data/upgrade.db`），容器/进程重启后槽位、版本、摘要、确认代次均保留。

### 一次性验收服务 verify

```bash
docker compose run --rm verify
```

该服务在 Compose 网络内执行：

1. `pytest` 代码测试；
2. `npm run build` 构建页面；
3. 启动**真实 uvicorn**，对三种断电恢复、损坏候选、并发 409 裁决、切换后重开一致性进行 HTTP 冒烟；
4. 执行完毕**自行退出**，全部通过退出码为 0，任一失败非 0。

## 本地开发（无 Docker）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cd web && npm install && npm run build && cd ..
DATA_PATH=./data/upgrade.db uvicorn backend.api:app --reload
# 或一键验收（自动建临时库、起服务、冒烟、清理）
bash scripts/verify.sh
```

## 页面操作流

1. **创建双槽设备**：A 槽为当前版本（带镜像摘要，代次 1，已确认），B 槽为空。
2. **提交更高版本候选**：可勾选故障点（候选写入 / 摘要校验时断电）或“损坏镜像字节”。
3. **模拟断电 → 重新打开设备**：查看恢复裁决报告（选定槽位、合格槽位、逐槽诊断、防回退理由）。
4. **确认切换**：原子提交，活动槽切换、代次 +1，旧槽 `SUPERSEDED`；也可在提交前模拟断电。
5. **并发裁决**：两个“页面”以不同 request_id 同时提交不同版本，观察 200/409 车道与活动版本不变；确认获胜候选后断电重开，核对槽位/版本/摘要/代次完全一致。
6. 底部为追加写的持久化诊断证据。

## 主要 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| POST | `/api/devices` | 创建双槽设备（含当前版本与摘要） |
| GET | `/api/devices/{id}` | 设备视图（槽位/代次/资格/最近恢复报告） |
| POST | `/api/devices/{id}/candidate` | 提交候选（`fault_point`、`corrupt`、`request_id`） |
| POST | `/api/devices/{id}/confirm` | 确认切换（`fault_point=confirm_switch` 可注入断电） |
| POST | `/api/devices/{id}/power-off` / `power-on` | 模拟断电 / 重新打开（执行恢复裁决） |
| GET | `/api/devices/{id}/evidence` | 诊断证据与历次恢复报告 |
