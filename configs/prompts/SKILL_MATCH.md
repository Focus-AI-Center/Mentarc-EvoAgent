---
name: SKILL_MATCH
description: Select the skill that best matches the user's request
variables: [skills_description, user_input]
---

Select the best matching skill from this catalog:

{skills_description}

User request:
{user_input}

Return only the exact skill name, or null if no skill is suitable. Do not explain your choice.
