# 修改后的验证与打包

先读 [工程交接](../AGENT.md)。以下命令均在ChatGPT工程根执行，Python>=3.11、标准库；`-B`避免生成缓存。测试本身可能写artifacts，保留历史工件并使用新目录。

## 先针对，再全量

```bash
CORE_GEEK_DEBUG_LOG=off PYTHONPATH=src python3 -B -m unittest discover -s tests -p 'test_v07.py' -v
CORE_GEEK_DEBUG_LOG=off PYTHONPATH=src python3 -B -m unittest discover -s tests -p 'test_state.py' -v
```

按修改范围替换文件名，不必每个小改动都跑所有比赛。代码交付前完整回归：

```bash
CORE_GEEK_DEBUG_LOG=off PYTHONPATH=src python3 -B -m unittest discover -s tests -v
python3 -B docs/reference/check_docs.py
git diff --check -- .
```

test_http需要本机端口权限；环境不允许时准确报告受阻测试，不能写全通过。它会测试源码入口和当次新建包的解压入口，不允许拿旧包替代。109项是574235a的历史数量，不是未来应硬凑的固定指标。

## 本地对局与任务机制

```bash
PYTHONPATH=src python3 -B -m lab.evaluate --seeds 1,3 --rounds 1300 --output artifacts/handoff-next-matches
PYTHONPATH=src python3 -B -m lab.sandbox_scenario --output artifacts/handoff-next-sandbox
python3 -B tools/analyze_debug.py artifacts/handoff-next-matches/rounds.log
```

运行期间不改源码；summary记录source_unchanged和文件哈希，变化则不能作为同一版本结果。再次运行改目录名，避免日志追加混场。先查summary和失败反馈，再按回合查ndjson或rounds.log，不一次性读取整份大日志。

## 打包与本地服务

```bash
python3 -B tools/package.py --output artifacts/coregeek-next.tar.gz
CORE_GEEK_DEBUG_LOG=artifacts/local-next.log bash run.sh 8080
```

服务命令会持续运行，测试完关闭自己启动的进程。`run.sh`仅本地使用，不进平台包；平台提供启动脚本。包包含main3.py、pyproject.toml、src/agent/**/*.py、MANIFEST.json，不含实验室、对手、文档或日志。MANIFEST文件哈希才表示实际源码；base_commit不代表未提交改动已包含在该提交里。变更后重新打包，不沿用v0.7包名宣称新版本。

## 有效测试的要求

回归应从具体失败场景出发，检验可观察动作／持久目标／合法性／准时到位，而非重复实现公式。覆盖两侧镜像、边界回合、矿耗尽、动态阻挡、满包、无资金、无安全路、异步反馈。显式分清协议错误与动作失败；前者为零不代表后者为零。

文档-only交接可只运行文档检查与diff检查，不消耗完整比赛资源。修改模拟规则时按lab/AGENT.md先确认官方依据，对双方重新跑同版本结果。
