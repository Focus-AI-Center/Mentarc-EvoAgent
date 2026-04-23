---
name: skill-creator
description: This skill should be used when the user wants to "create a skill", "write a new skill", "improve skill description", "organize skill content", or needs guidance on skill structure, progressive disclosure, or skill development best practices.
triggers:
  - 创建技能
  - 保存技能
  - 新建技能
  - 做成技能
  - create skill
  - write skill
  - improve skill
required_tools:
  - skill_manager
source: builtin
is_builtin: true
tags:
  - meta
  - skill-management
---

# 技能开发指南

本技能提供创建和优化可复用技能的完整指导。

## 关于技能

技能是模块化的任务模式包，通过提供专业知识、工作流程和工具集成来扩展 agent 的能力。技能将通用 agent 转变为针对特定领域的专业 agent。

### 技能的价值

1. **专业工作流** — 特定领域的多步骤流程
2. **工具编排** — 多工具协作的执行模式
3. **领域知识** — 业务规则、模板、参考资料
4. **可复用资源** — 脚本、模板、示例数据

### 技能结构

每个技能是一个目录，包含必需的 SKILL.md 和可选的附属资源：

```
skill-name/
├── SKILL.md (必需)
│   ├── YAML frontmatter — 元数据
│   └── Markdown body — 执行步骤
└── 附属资源 (可选)
    ├── references/   — 参考文档（agent 按需加载到上下文）
    ├── scripts/      — 可执行脚本（agent 通过 execute_code 运行）
    ├── assets/       — 输出资源（模板、图片等）
    └── examples/     — 示例文件
```

### 渐进式加载

技能采用三级加载策略管理上下文效率：

1. **元数据**（name + description + triggers）— 始终在上下文中（~100 词）
2. **SKILL.md body** — 技能匹配激活时注入系统提示词（建议 < 3000 词）
3. **附属资源** — agent 按需加载（通过 `skill_manager(action="get")` 查看，或 `execute_code` 执行脚本）

## SKILL.md 规范

### Frontmatter 字段

```yaml
---
name: write-seo-report            # 必填，小写+中划线
description: 描述技能用途和适用场景    # 必填，一句话说明
triggers:                          # 必填，3-5 个触发词
  - 写报告
  - generate report
required_tools:                    # 推荐，技能需要的工具
  - websearch
  - fetchurl
tags: [writing, seo]               # 可选，分类标签
source: user_defined               # extracted | user_defined | builtin
---
```

**name**: 小写字母 + 中划线，2-4 个英文单词。如 `write-seo-report`、`generate-weekly-summary`。

**description**: 具体说明用途和场景，让匹配系统能准确识别。
- 好：`根据关键词搜索资料并生成 SEO 优化的分析报告`
- 差：`帮助用户完成各种任务`

**triggers**: 覆盖不同表达方式（正式/口语/中文/英文）。不需要太多，embedding 和 LLM 匹配会兜底。触发词用于三阶段匹配的第一阶段（关键词匹配，< 1ms）。

### 可用工具

技能可在 `required_tools` 中引用以下工具（系统会在技能激活时自动加载缺失的工具）：

| 工具 | 用途 |
|------|------|
| `websearch` | 搜索网页信息 |
| `fetchurl` | 抓取 URL 内容 |
| `imagegeneration` | 生成图片 |
| `imageediting` | 编辑图片 |
| `execute_code` | 执行 Python/Bash 代码 |
| `update_user_profile` | 更新用户画像 |
| `update_memory` | 读写长期记忆 |
| `skill_manager` | 管理技能 |

### Body 编写规范

1. **使用编号步骤**，每步一个明确动作
2. **说明"为什么"**，不只是"做什么"
   - 好：`1. 使用 websearch 搜索关键词（获取最新信息，避免过时数据）`
   - 差：`1. 搜索关键词`
3. **指明工具和输出格式**
   - `2. 使用 fetchurl 抓取前 3 个结果的正文内容`
   - `5. 输出格式：Markdown，含标题、摘要、关键发现、参考链接`
4. **处理边界情况**
   - `如果搜索结果不足 3 条，使用所有可用结果`
5. **引用附属资源**
   - `参考 references/report-template.md 中的模板格式`
   - `执行 scripts/validate.py 检查输出质量`
6. **保持精简**，通常 10-30 行，不超过 50 行

### 附属资源使用

**references/** — 参考文档，agent 通过 `skill_manager(action="get", name="技能名")` 查看全部内容。适用于：数据库 schema、API 文档、详细模式说明、公司规范。保持 SKILL.md 精简，把详细内容放这里。

**scripts/** — 可执行脚本，agent 通过 `execute_code` 运行。适用于：需要确定性结果的重复代码、验证工具、数据处理脚本。

**assets/** — 输出资源，不加载到上下文，而是在输出中使用。适用于：模板文件、品牌素材、样板代码。

**examples/** — 示例文件，展示技能的输入输出。适用于：完整的使用示例、配置样例。

SKILL.md 中必须写明如何引用这些资源，否则 agent 不会知道它们的存在。

## 什么值得做成技能

**适合：**
- 涉及 2 次以上工具调用的多步骤流程
- 有明确的输入→处理→输出模式
- 未来大概率会重复使用
- 需要特定的步骤顺序和格式要求

**不适合：**
- 一次性的简单问答或闲聊
- 单轮对话即可完成的任务
- 仅涉及单次工具调用（一次搜索、一次图片生成）
- 过于依赖特定上下文，无法泛化

## 创建流程

### 第 1 步：理解技能用例

分析具体使用场景：
- 用户会怎样触发这个技能？典型的表达方式有哪些？
- 需要用到哪些工具？调用顺序是什么？
- 每步的输入和输出是什么？
- 有没有需要用户提供的可变参数？
- 是否需要参考文档、脚本或模板？

与用户确认后再继续。

### 第 2 步：规划资源

分析每个用例，确定需要哪些可复用资源：
- 是否有重复编写的代码 → `scripts/`
- 是否需要参考文档（schema、API 规范等）→ `references/`
- 是否需要模板或素材 → `assets/`
- 是否需要示例 → `examples/`

### 第 3 步：提交技能候选

使用 `skill_manager` 提交候选：

```
skill_manager(
  action: "save",
  name: "write-seo-report",
  description: "根据关键词搜索资料并生成 SEO 优化的分析报告",
  triggers: ["写报告", "生成报告", "write report"],
  instructions: "（完整的执行步骤）",
  required_tools: ["websearch", "fetchurl"],
  tags: ["writing", "research"]
)
```

候选技能保存后需要用户确认才会生效。

### 第 4 步：添加附属文件

如果技能需要参考文档、脚本或模板，使用 `ref_set` 添加：

```
skill_manager(
  action: "ref_set",
  name: "write-seo-report",
  ref_filename: "references/report-template.md",
  ref_content: "（模板内容）"
)
```

可以添加多个文件到不同子目录。

### 第 5 步：保存技能

提交后技能立即生效，未来对话中用户说出触发词时会自动激活。

### 第 6 步：迭代改进

使用同名 `save` 提交更新版（系统自动版本递增），提交的是**完整定义**，不是增量修改。

## 验证清单

创建技能前检查：

- [ ] name 使用小写+中划线格式
- [ ] description 具体明确，不笼统
- [ ] triggers 覆盖中英文和口语表达（3-5 个）
- [ ] required_tools 列出所有需要的工具
- [ ] instructions 使用编号步骤，每步有明确动作
- [ ] instructions 精简（10-30 行）
- [ ] 附属文件在 instructions 中有明确引用
- [ ] 边界情况有处理说明

## 参考资源

- **`references/skill-creator-original.md`** — 通用技能编写指南，介绍组织结构、操作步骤和验证要求
