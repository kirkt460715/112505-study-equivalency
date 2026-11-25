# 跨村研学：出席 · 互认 · 核验 · 发证后端

纯 Python 3.11 标准库实现，无第三方依赖。在给定的 `fixtures/` 演练数据上，
把「出席记录 → 身份归并 → 替代课互认 → 能力点核验 → 结业发证 → 纠正复议 →
回执」整条链路做成可调用的服务，并对题目中的每条业务红线都有自动化测试。

## 运行

```sh
python3 -m unittest -v                 # 全部 41 个测试（含原有 2 个）
python3 -m studyvisit scenario         # 重放 fixtures，打印权威判定 JSON
python3 -m studyvisit serve --port 8080
python3 backend.py                     # 等价于上面的 serve
```

`scenario` 会在进程内启动 `fixture_gateway.FixtureGateway`，真实演示
`lost_reply` 后用 `GET /v1/receipts/<reference>` 找回回执的过程。

## 演练数据的权威结论

按业务分钟重放（5/8/10/15/16 提交 → 17 分发证 → 28 分纠正）：

| 对象 | 结论 |
| --- | --- |
| `l-2`（school-18） | 发证时三项全齐；`obs-18` 被纠正为 `departed_early` 后，仅缺 `guide-confirm`（观察模块），`hands-on`（`do-14`）与 `separate-reflection`（`ref-9`）仍由**两条相互独立的记录**支撑。`cert-7` 进入 `in_review / 复议中`，应「复议或补观察活动」。 |
| `l-1`（school-18） | 尚无记录，三项全缺，按当前证据判定未结业。 |
| `l-3`（school-21） | 桥接 `guest-token-x → [l-2, l-3]` 只是**待核候选**；身份事件确认给 `l-2` 后，`do-14` 只归 `l-2` 一次，`l-3` 不会自然分到该签到，三项全缺。替代课 `alt-art` 也只对 `school-18` 生效。 |
| `cert-7` 的依据 | 冻结为发证当时的 `[obs-18, do-14, ref-9]` 与当时政策快照，`basis_was_sufficient_at_issue=true`，永不被后续纠正/政策调整改写。 |
| 迟到回执 | `publish-certificate → school-18` 首次发送 `lost_reply`，用同一 `reference` 重试/查询找回 `acknowledged:true`；回执绑定到原决定 `cert-7#issued@17`，**不会**让已 `in_review` 的证明恢复可用。 |

学校可调用 `GET /api/school/review-board` 一眼看到：哪些证件仍有其他独立支撑、
哪些（`cert-7`）需要正式复议 + 补 `observe` 活动。

## 需求 → 实现 → 测试 追溯

| 题目要求 | 实现位置 | 主要测试 |
| --- | --- | --- |
| 每团登记学习要求与最低证据 | `service.register_adoption(minimum_evidence=...)`，按 cohort 覆盖；`domain.build_adoption` 从 `goals` 播种 | `test_school_registers_higher_minimum_evidence` |
| 讲解点提交实际学生与环节证据 | `service.submit_evidence` / `submit_evidence_batch`；`POST /api/evidence[/batch]` | `test_batch_*`、API 批量用例 |
| 项目可能只支持一部分要求 | `substitution.supports` 显式能力点；`alt-art` 仅 `hands-on` | `test_every_student_verdict` |
| 一条记录可证多个普通能力点 | `domain.evaluate_evidence` 允许普通能力点共用 `action_id` | `test_one_record_may_prove_two_ordinary_points` |
| 独立练习不能一次动作抵两次 | 独立/专用证据分配器：独立学分动作全局唯一，`dedicated_evidence` 独占；支持 `min_count>1` | `test_one_action_cannot_count_twice_*`、`test_dedicated_reflection_needs_its_own_record` |
| 教研组补批替代 | `add_substitution(retroactive=True, effective_from_minute=...)` | `test_retroactive_substitution_helps_unfinished_student_only` |
| 教研组缩小适用范围 | `adjust_substitution(change="limited_scope" | supports | cohorts)`，收窄只做交集、不扩张 | `test_narrowing_substitution_*`、`test_academic_adds_and_narrows_substitution` |
| 未结业者按当前证据核验 | `evaluate_student` / `GET /students/{id}/verification` | `test_every_student_verdict` |
| 已发证保留当时依据 | `cert.basis_snapshot`（证据状态 + 判定 + 政策）不可变 | `test_issued_cert_keeps_frozen_basis_but_enters_review` |
| 纠正后区分「仍有独立支撑 / 需复议补活动」 | `assess_certificate` + `review_board`：`point_support`、`unsupported_points`、`independent_support`、`required_actions` | `test_cert_stands_when_corrected_proof_has_independent_backup`、`test_school_review_board_*` |
| 补活动后经学校决定才能结束复议 | 新证据不自动改状态；`reaffirm_certificate` 重新核验全齐才 `reaffirmed` | `test_supplementary_activity_then_reaffirm` |
| 已核实环节随学生到下一站 | `passport` / `transfer` 返回可携带的已核实能力点与证据 | `test_transfer_carries_verified_parts_and_mandate` |
| 接手讲解点只能补获准环节 | `partners` 授权范围 + `partner_uncovered` 校验，越权 `403 partner_not_authorized` | `test_partner_cannot_submit_unauthorised_segment` |
| 重新分班不合并签到 | `reclass_student` 只移动单个 `student_id` 外键，不复制 | `test_reclass_keeps_evidence_on_one_student` |
| 补姓名对应不把同一签到并给两人 | 桥接候选不产生归属；`confirm_identity` 单一归属且幂等；更正时「移动」而非「复制」 | `test_one_signin_attributed_once_*`、`test_identity_correction_moves_single_signin_never_copies` |
| 迟到回执属于原发证决定、不能复活复议件 | `bind_receipt(bound_to_decision="cert-7#issued@17")` 绝不改状态；`GatewayClient.publish_with_recovery` 处理丢回执 | `test_lost_reply_is_recoverable_and_late_ack_does_not_restore` |
| 带队老师补办下一批学生 | `enroll_students` 批量、幂等；可带过去 `minute` 补交；证据也有批量接口 | `test_teacher_registers_next_batch_idempotently` |
| 学校查询发证依据 | `GET /api/certificates/{id}/basis`、`GET /api/school/review-board` | `test_school_queries_issuance_basis` |
| 合作方只看当次交流需处理的记录 | `create_exchange` + `partner_exchange_view` 严格按 partner/ subjects / 仅本点记录；`partner_worklist` 行级收敛 | `test_partner_worklist_is_row_scoped`、`test_exchange_*`、`PartnerMinimizationTest` |
| 编号复用保护 | 同一 `reference` 不同操作 → `ReferenceReused` / HTTP 409 | `test_reference_reuse_for_other_operation_conflicts` |

## 模块

- `studyvisit/domain.py`：纯规则——范围授权、替代课适用、能力点分配/防一次抵两次。
- `studyvisit/service.py`：JSON 可序列化的有状态用例门面（加锁、可 `save/load`）。
- `studyvisit/scenario.py`：按业务分钟重放 `fixtures/`，产出权威报告。
- `studyvisit/gateway_client.py`：台账/事件拉取与回执幂等重试、`lost_reply` 找回。
- `studyvisit/server.py`：标准库 `ThreadingHTTPServer` JSON API。
- `studyvisit/__main__.py`：`scenario` / `serve` 命令；根目录 `backend.py` 为快捷入口。

## HTTP 示例

```sh
# 学校查发证依据（含冻结快照与当前支撑比对）
curl -s localhost:8080/api/certificates/cert-7/basis | python3 -m json.tool

# 学校的纠正影响看板
curl -s localhost:8080/api/school/review-board

# 学生当前核验 / 可携带 passport
curl -s localhost:8080/api/students/l-2/verification
curl -s localhost:8080/api/students/l-2/passport

# 带队老师补办下一批（幂等）
curl -s -X POST localhost:8080/api/cohorts/school-18/students/batch \
  -H 'Content-Type: application/json' \
  -d '{"student_ids":["l-4","l-5"]}'

# 教研组补批 / 收窄替代课
curl -s -X POST localhost:8080/api/academic/substitutions -H 'Content-Type: application/json' \
  -d '{"course":"alt-x","supports":["hands-on"],"cohorts":["school-18"],"retroactive":true}'
curl -s -X PATCH localhost:8080/api/academic/substitutions/alt-art \
  -H 'Content-Type: application/json' -d '{"change":"limited_scope","supports":["hands-on"]}'

# 讲解点提交环节（site-b 越权代讲 observe 会得到 403）
curl -s -X POST localhost:8080/api/evidence -H 'Content-Type: application/json' \
  -d '{"evidence_id":"e-1","student":"l-2","partner":"site-b","course":"alt-art"}'

# 合作方只看本站、本次交流
curl -s "localhost:8080/api/partner/worklist?partner=site-b"
curl -s "localhost:8080/api/partner/exchanges/ex-1?partner=site-b"
```

## 设计要点

1. **两套时间视角分离**：`basis_snapshot` 是发证当时的不可变依据；
   `current_corroboration` 用当前证据与当前教研政策重算，只用于复议/补活动建议。
2. **身份是单值外键**：桥接表的多候选不会复制签到；确认/更正都是把同一条
   证据记录**归属/移动**到唯一学生，从结构上杜绝「同一签到并给两人」。
3. **证据 vs 决定**：补交证据只改变 `current_*`，是否结束复议必须由学校
   `reaffirm`；回执只是对原发送动作的确认，二者都不能自动改写证明状态。
4. **合作方最小可见**：交流视图只返回该 partner 自己提交的记录与待办
   （如 `confirm_identity`、`corrected:<receipt>`），跨 partner 访问返回 403。
