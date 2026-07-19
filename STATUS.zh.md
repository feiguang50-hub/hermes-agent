# Hermes Agent — Curator 自我改进评估机制

最后更新:2026-07-19

本项目为 Hermes Agent 的 curator 加了一条"自我改进的评估闭环":把技能的实际使用效果记录下来,合成 0–1 质量分,再反过来驱动 curator 的 keep / split / deprecate / archive 决策。整条链路在两批真实 export + 一批合成数据上反复验证过。

## 已完成

分四类:**核心机制 / 防护 / 可观测 / 测试**。

### 核心机制:split / deprecate 生命周期
- `agent/curator.py` —— 加 `STATE_SPLIT` / `STATE_DEPRECATED`、lifecycle 抽取与调和、structured summary 解析、prompt 里"按 score 决策"的引导。
- `tools/skill_manager_tool.py` —— `SKILL_MANAGE_SCHEMA` 加 `split` / `deprecate` + 工具调用支持。
- `hermes_cli/subcommands/skill.py` —— `hermes skill score <名字>` 子命令(读评分,只读)。

### 反馈闭环接通(本轮新增,两段都接)
- **输入端** —— `record_outcome` / `record_user_feedback` 之前**从未被运行时调用**;现在接上了:
  - `tools/skill_usage.py` 加 per-turn 技能集(ContextVar,`bump_use` 自动喂入)+ `infer_turn_outcome`(按 turn 结束信号映射 outcome)+ `record_turn_skill_outcomes`(curator 后台评审时跳过,避免伪造)。
  - `agent/turn_context.py` 在 turn 开头启动追踪;`agent/turn_finalizer.py` 在 turn 结束前对每个用过的技能记一条 outcome。
- **输出端** —— score 之前在 prompt 里让 LLM "调 `compute_skill_score`",但 LLM 没有工具能调 Python 函数,实际看不到 score;现在接上了:`_render_candidate_list` 把 `quality=0.xx  ok=success/resolved  fb=up/down` 直接写进候选清单;prompt 改成"读这些列"。

### 防护层(curator dry-run / retention 守卫)
- `agent/curator_hooks.py` —— 三层守卫:hard-block dry-run、keyword retention、terminal skill-mutation 检测。
- `_MUTATING_ACTIONS` 加 `edit` / `remove_file`(原守卫漏了这两个,真实数据发现)。
- 嵌套技能目录 + CJK 关键词提取(真实数据发现:之前的扁平路径解析导致守卫在嵌套布局上完全失效)。

### 可观测层
- `hermes_cli/curator.py` —— `hermes curator audit`(读 audit 日志)、`hermes curator backup` / `rollback`(快照+回滚)、`hermes curator run --dry-run --consolidate`。
- `agent/curator_backup.py` —— 预运行自动快照 + 安全回滚。
- 自动确定性 prune 跳过 `split` / `deprecated` 状态(R7:之前会把它们静默归档,毁掉指针)。

### 真实数据验证(全部 dry-run,无 `--apply` 写入)
| 轮 | 数据 | 关键验证 |
|---|---|---|
| 5–6 | 5 技能 fixture + 边界 fixture | 决策质量、回滚、LLM 上下文行为 |
| 7 | 5 技能 fixture | 真实 LLM `--apply`:deprecate 全链路(工具调用→落盘→回滚)验证通过 |
| 8 | 25 技能真实 export(76 文件) | dry-run 暴露 2 个新问题(见下) |
| 9 | 同 8 | #15 / #16 修复后复验:consolidation 浮现(1)、嵌套关键词(15-23 词) |
| 10 | 123 技能未清理脏数据(712 `.archive` + 130 记录 / 7 路径前缀键) | 全部清干净:0 报错、模型推理正常、guard 不触发(YAML 通道) |
| **2b** | **3 技能合成(1 个被刻意造差) | 反馈闭环端到端:LLM 读了候选清单里的 `quality=0.09` / `ok=3/20` / `fb=0/8`,deprecate 了**只**那个差的,replaced_by 指向好的,reason 明确引用了 score/thumbs — 闭环真的驱动决策了** |

## 当前状态

- **数据闭环两端接通**(本轮新增):record_outcome 已在 turn 结束自动跑;候选清单里能看见 quality + ok + fb 字段。**接下来需要真实使用攒数据**,评分才能真正反映"技能有没有帮上忙"。
- **P0 全部修完**:#13、#7、#15、#16 全部修复+测试+真实数据复验。
- **2b 已证**:低质量技能在真实 LLM 决策中**确实**被 deprecate 掉,质量信号被采纳。

## 已知问题(按优先级)

### P0 — 已修但留有观察项
1. ✅ **#15 dry-run 漏报 consolidation 提案** —— 修法:把 YAML block 里的 `consolidations:` / `prunings:` 提案并入 dry-run 计数/数组,标 `proposed`,放在 cron 重写**之后**确保 dry-run 不改 cron。真实数据复验:0→正确数。
2. ✅ **#16 keyword-retention 守卫在嵌套/CJK 上失效** —— 修法:`_load_skill_keywords` 加嵌套感知回退(用 `skill_usage._find_skill_dir`)。CJK 分词仍是单字(P2 范畴)。
3. ✅ **R7 确定性 prune 覆盖 split/deprecated 状态** —— prune 跳过这两种状态。
4. ✅ **R13 `_MUTATING_ACTIONS` 漏 `edit` / `remove_file`** —— 已补。
5. ✅ **R15 staged-replay 丢 `split_into` / `replaced_by`** —— 已补。

### P1 — 待做
6. **`split` 工具调用链路仍未真实验证** —— 七轮 apply 没触发,改由专门设计 fixture 验(原 KNOWN_ISSUES 旧版有详细描述)。
7. **A/B rubric 洞** —— Fixture A 倾向 merge、Fixture B 倾向 keep,候选 prompt 修订已记,未应用。
8. **keyword-retention 守卫拦合法 umbrella 补内容** —— 4 次 patch 都被守卫误杀,umbrella 内容缺。候选取舍:non-interactive 审批、保留率改为保留度量、偏好 `edit`。
9. **section E 0.5 阈值无程序化支撑** —— 评分和阈值是脱节的,LLM 自己做门槛判断。
10. **`curator.py:2084` 宽 `except Exception`** —— 把 LLM pass 一切错误降级为 debug 日志,P0-③ 的诊断就是被它掩盖的。

### P2 — 长期
11. **D 任务:技能检索层** —— 元数据索引 + 可选 embedding + **CJK 分词器**(真实数据验证:中文被切成单字,守卫对中文技能弱)。规模化到上百技能前的硬性前置。
12. **清理债** —— 死代码(`_resolve_review_model`、`auto_summary` 形参、`summary_so_far`、`score_many`)、过期 docstring、重复的 `_CONTENT_FIELDS` / 截断阈值、未指定 `encoding="utf-8"` 的 `read_text()`。

## 分支状态

`main`,`feiguang50-hub/hermes-agent`。curator 生命周期工作总共 **16 个 commit、~900 行生产代码 + ~1800 行测试**。其中 6 个 commit 是数据驱动的修复/验证(对真实 export 的回应),其余 10 个是初始机制构建。关键 commit:

```
bb55094 fix(agent/curator): surface quality score into candidate list (output end)
f1d50b8 feat(skill-usage): wire skill-outcome sensor into the turn lifecycle
3f5e8b2 test(skill-scoring): pin that real outcome/feedback discriminates score
59bf254 fix(agent/curator-hooks): resolve nested-category skill paths (#16 / P0)
a139078 fix(agent/curator): surface consolidation proposals in dry-run report (#15 / P0)
69c763d fix(agent/curator): prune must not archive split/deprecated skills (R7)
450480a fix(agent/curator-hooks): gate edit/remove_file in dry-run guard (R13)
a6e06e1 fix(tools/skill-manager): forward split_into/replaced_by in staged replay (R15)
0d81bb7 feat(agent/curator): surface split and deprecate in structured summary
f7ea70d feat(tools/skill-manager): wire split/deprecate through schema
4432c9e feat(agent/curator): integrate split/deprecate vocabulary into review prompt
```

## 验证方法

- 全部 P0 / P1 修复都附回归测试,且**已验证"去掉修复就失败"**。
- 所有"接了真实数据就崩溃"的潜在问题都跑了真实 export 验证。
- **2b 端到端**:用合成数据(1 个 quality=0.09 / 1 个 1.0)跑真实 LLM dry-run,LLM 读了 inline 列后,deprecate 了**只**那个差的,replaced_by 指向好的,reason 明确引用 "abysmal quality (3/20 success)" / "6/0 thumbs up"。**闭环端到端驱动决策,已证实。**
- **真实数据 / 闭环接通后形态**:同一批 27 技能真实数据上跑两次,LLM 一次 keep all、十轮那次 deprecate `shopping-agent` 进了 `remote-access-setup` umbrella——**同一数据、同一模型、相反结论**,实证了 Fixture B rubric 洞(LLM 在边界案例上的不稳定性)。这正是"接了传感器但数据为零"的真实形态:quality 列没数据时 LLM 退回 activity + prompt 规则,边界案例就会随机。**真实使用累积 outcome/feedback 之后,quality 列开始区分好坏,决策会稳定下来。**
- 真实数据 5-fixture `--apply` 端到端(deprecate 全链路):通过 + rollback 字节级一致。

## 测试结果

- **curator 作用域全部测试(本机 UTF-8 环境):343 通过**。
- 全项目套件在本机 Windows cp932 + 缺可选依赖(`prompt_toolkit` / `acp` / `pytest_asyncio` / `jwt` / `mcp` / `cryptography` / `wcwidth` / `setuptools`)的环境下不可信——污染与 curator 无关,应在 CI / 正常环境跑完整套件。
