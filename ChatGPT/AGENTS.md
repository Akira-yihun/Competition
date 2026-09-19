# ChatGPT 工程目录约束

依据用户 2026-09-19 的明确要求：本 Agent 的代码、文档、测试及工件写入范围仅限 `/Users/yihun/Code/Project/2026 HW Comp/CoreGeek/ChatGPT/`。

- 工作区原始 `docs/`、`CoreGeek/DeepSeek/` 及其他目录只能读取，不得修改、移动、删除或格式化。
- 上级 AGENTS.md 中面向 DeepSeek Agent 的写入范围不适用于用户明确指定的本工程任务；遵循用户当前指令，不转去修改 DeepSeek。
- 保留其他任务产生的改动；检查和暂存时限定本目录，不执行仓库级批量覆盖或清理。
- 每轮优化在本目录 `docs/design/` 记录需求、规则依据、方案、验证结果与未验证假设。
- 用户要求先评审方案时，仅交付文档，待后续实施指令后再修改代码。本轮方案见 `docs/design/07-v06候选优化方案评审.md`。
