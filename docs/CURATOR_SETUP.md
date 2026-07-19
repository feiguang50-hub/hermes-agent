# Curator 部署自检 (Deploy & Verify)

你把这份改动部署到服务器后,按下面顺序跑一遍——能确认闭环是否真的接上。

## 1. 拉代码并重启

```bash
cd /path/to/your/hermes-agent
git pull origin main
# 重启 Hermes (按你的部署方式)
```

## 2. 一行自检:outcome 传感器接好没

```bash
PYTHONUTF8=1 python -c "
from tools.skill_usage import begin_turn_skill_tracking, bump_use, get_turn_skills_used, infer_turn_outcome
begin_turn_skill_tracking()
bump_use('demo')
print('tracking works:', sorted(get_turn_skills_used()))
print('infer examples:', infer_turn_outcome(final_response='ok'),
                          infer_turn_outcome(interrupted=True, final_response='x'))
"
```

预期输出:
```
tracking works: ['demo']
infer examples: success abandoned
```

看到 `success` 和 `abandoned` —— 推断函数也通了。

## 3. 检查 locale (cp932 / latin1 可能踩坑)

服务器如果不是 UTF-8 locale,我们的代码里有几处 `read_text()` 没指定 encoding。**一行查清:**

```bash
python -c "import locale; print(locale.getpreferredencoding())"
```

- 输出 `utf-8` / `UTF-8` → 无忧
- 其他(尤其 `cp1252` / `gbk` / `cp932`)→ 在每条 `hermes` 命令前加 `PYTHONUTF8=1`,或在 `~/.bashrc` 加 `export PYTHONUTF8=1`

## 4. 检查 GitHub 可达 (国内常出问题)

```bash
curl -sS -o /dev/null -w "%{http_code}\n" --max-time 5 https://github.com
```

- `200` / `301` → 通
- 超时 / `000` → 需配代理(国内场景常见),参考 `git config --global http.proxy http://127.0.0.1:<你的代理端口>`

## 5. 跑一次 curator dry-run

```bash
hermes curator run --dry-run --consolidate
```

看报告 `logs/curator/<时间戳>/REPORT.md`:
- 候选清单里每个技能应该带 `quality=X.XX ok=S/N fb=U/D` 三列(说明 LLM 看得到 score 了)
- 报告顶部有 `> **DRY-RUN preview — read-only.**` 横幅(说明 dry-run 标记生效)
- 状态(打开 `~/.hermes/skills/.usage.json`):**老技能 quality ≈ 1.0**(还没 outcome 数据)、use_count 高的技能有值

## 6. 正常用一段时间

不需要任何特殊操作。**每次有人用 Hermes 完成一次对话,turn 结束时自动给本轮用过的每个技能记一条 outcome**(success / failure / abandoned / unknown)。过几天到一周,`.usage.json` 里 outcome 就有形状了。

## 7. 数据累积后,再跑一次

```bash
hermes curator run --dry-run --consolidate
```

这次 LLM 的决策应该**明确引用候选清单里的 quality 列**——比如"quality=0.12, ok=3/20 → deprecate"。如果 LLM 还在凭内容/活动计数做决策、没引用 quality,先看报告里候选清单是不是真的有 quality 列(可能 locale 还是有问题)。

## 不该发生的事(出问题对照)

| 现象 | 原因 | 修法 |
|---|---|---|
| 第 2 步报 `ImportError` | 依赖没装 / Python path 不对 | `pip install -e .` |
| 第 2 步 `tracking works: []` | 正常——`bump_use` 在没开始追踪时是空集 | 先调 `begin_turn_skill_tracking()` |
| 第 5 步 `quality=` 列缺失 | 老版本代码没这个列 | `git status` 看是不是真的拉到最新 |
| curator 报 "provider deepseek needs API key" | 老的 `~/.hermes/config.yaml` 是另一份 | `python -c "import os; os.environ['HERMES_HOME']; print(os.environ.get('HERMES_HOME'))"` 确认读的是预期 home |
| `--apply` 后技能没真改 | 预期——`--consolidate` 会在 dry-run 模式下什么也不改 | 想真改去掉 `--dry-run` 跑,**先备份 `.usage.json` 和 skills/ 再 apply** |

## 想自己积累点数据试试

不想等真实用户用,在你服务器本机直接合成数据灌进 `.usage.json`:

```python
from tools import skill_usage as su
import agent.skill_scoring as sc
import json

# 造一个"用过的差技能"
su.mark_agent_created("my-skill")
for _ in range(3): su.record_outcome("my-skill", "success", source="auto")
for _ in range(17): su.record_outcome("my-skill", "failure", source="auto")
for _ in range(8): su.record_user_feedback("my-skill", "down", "注释可选")

print(sc.compute_skill_score("my-skill")["score"])
# 期望 ~0.12 (远低于 deprecate 阈值 0.5)
```

然后 `hermes curator run --dry-run --consolidate` 应该看到 LLM deprecate 它并 replaced_by 到合理的目标。
