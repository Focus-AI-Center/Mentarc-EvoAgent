---
name: SKILL_MATCH
description: LLM 兜底匹配用户输入到已有技能
variables:
  - user_input
  - skills_description
---

判断用户意图是否可以通过以下技能之一来完成。
即使用户没有使用触发词，只要意图与技能功能相关，就应该匹配。

## 用户输入
{user_input}

## 可用技能
{skills_description}

## 输出规则
- 如果用户意图可以由某个技能完成，输出该技能的 name（如 write-seo-report）
- 如果没有合适的技能，输出：null
- 只输出技能名或 null，不要任何解释
