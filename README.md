# 轨道载荷 A/B 双槽镜像升级 — 断电安全验收台

模拟轨道载荷维护员的启动镜像升级流程。核心安全保证：

- **每次校验都以本次实际写入的镜像字节为准**：不沿用任何候选先前的校验结论——即使新版本候选声明的摘要与旧镜像完全相同，也会重新测量本次字节，不符即在提交阶段拒绝（`REJECTED`），不推进确认代次、不切换活动槽位；
- **任意时点断电都不会引导摘要不符或未确认的候选**；
- **重开即审计**：即使是已确认（`CONFIRMED`）槽位，每次上电都重新测量其持久化字节；摘要不符立即隔离为 `REJECTED`（保留证据），绝不引导；
- **新版本生效后永不回退**（旧槽位标记 `SUPERSEDED`，恢复时永不选择）；若审计后再无合格槽位，设备安全收敛为「拒绝引导/维修态」，而不是回退旧版本；
- 恢复时**仅从「字节审计通过、清单完整且已确认」的槽位中选定唯一活动槽位**，并展示逐槽诊断证据与裁决理由；
- 两个页面并发提交不同候选时，**仅一个请求取得当前代次的升级资格**，另一个得到稳定 `409` 且不改写活动版本。

## 架构

```
backend/          FastAPI 服务
  models.py       槽位/设备/恢复报告领域模型（EMPTY→CANDIDATE→VERIFIED→CONFIRMED / REJECTED / SUPERSEDED，含重开审计隔离标记）
  versioning.py   点分数字版本比较（候选必须严格更高）
  store.py        SQLite(WAL) 持久化：槽位清单、候选阶段、确认代次、资格令牌、诊断证据、镜像 BLOB
  service.py      升级编排：代次资格、本次字节摘要校验（不沿用旧结论）、原子确认切换、断电恢复裁决与重开字节审计
  api.py          HTTP API + 托管 web/dist 静态页面
web/              Vite 原生 JS 前端（中文界面，全部操作经真实 API）
tests/            pytest（19 个用例：三种断电、损坏候选、并发裁决、防回退、重开一致、旧摘要复用攻击、已持久化异常槽重开隔离）
scripts/
  verify.sh       一次性验收：pytest → 构建页面 → 真实 uvicorn → HTTP 冒烟
  smoke_http.py   断电恢复、并发裁决与旧摘要/持久化异常的 HTTP 冒烟（100+ 条断言）
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
| 候选字节损坏 / 沿用旧摘要 | 清单摘要 ≠ 本次写入实测摘要，槽位 `REJECTED`，证据保留 | 诊断 `digest_mismatch`，永不引导 |
| 已确认槽持久化内容被篡改 | 清单为 `CONFIRMED` 但 BLOB 实测摘要不符 | 重开字节审计先隔离为 `REJECTED`（诊断 `confirmed_digest_mismatch`，`quarantined=true`）；无其它合格槽时设备拒绝引导且不回退 `SUPERSEDED` 旧版本 |

校验结论不做任何跨候选缓存：摘要校验与确认前复检都直接对本次持久化字节重新取 SHA-256；上电恢复还会对每个 `CONFIRMED` 槽再做一次字节审计。所有变更在 SQLite `BEGIN IMMEDIATE` 事务内完成，`COMMIT` 是唯一原子切换点；并发提交由数据库写锁串行化后再做代次资格裁决，因此冲突结果稳定。

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
3. 启动**真实 uvicorn**，对三种断电恢复、损坏候选、并发 409 裁决、切换后重开一致性、**旧摘要复用攻击拒绝**与**已持久化异常槽重开隔离**进行 HTTP 冒烟；
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
5. **沿用旧摘要攻击验收**：完成一次正常升级并确认后，勾选「沿用前一镜像摘要」，以更高版本提交字节不同的候选——候选必须被拒绝，代次/活动槽不变。
6. **已持久化异常状态重开裁决**：正常升级确认后点「篡改已确认活动槽持久化字节」，再重新打开设备——该槽被隔离、设备拒绝引导且不回退；再次重开裁决一致。
7. **并发裁决**：两个“页面”以不同 request_id 同时提交不同版本，观察 200/409 车道与活动版本不变；确认获胜候选后断电重开，核对槽位/版本/摘要/代次完全一致。
8. 底部为追加写的持久化诊断证据。

## 主要 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| POST | `/api/devices` | 创建双槽设备（含当前版本与摘要） |
| GET | `/api/devices/{id}` | 设备视图（槽位/代次/资格/最近恢复报告） |
| POST | `/api/devices/{id}/candidate` | 提交候选（`fault_point`、`corrupt`、`digest`、`request_id`） |
| POST | `/api/devices/{id}/confirm` | 确认切换（`fault_point=confirm_switch` 可注入断电） |
| POST | `/api/devices/{id}/fault/tamper-confirmed` | 故障注入：篡改已确认槽持久化字节而清单摘要不变，并断电 |
| POST | `/api/devices/{id}/power-off` / `power-on` | 模拟断电 / 重新打开（执行恢复裁决与字节审计） |
| GET | `/api/devices/{id}/evidence` | 诊断证据与历次恢复报告 |
