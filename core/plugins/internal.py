"""Internal tools - always loaded, not part of plugin registry.

These tools allow the Agent to self-manage user profiles, skills,
and memory during conversation.
"""

import json
import re
from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from core.agent_compat import BaseTool
from core.skills.manager import SkillManager
from utils.logger import logger
from utils.workspace import Workspace
from utils.ws_call import ws_call

# --- User Profile Tool ---


class UpdateUserProfileParams(BaseModel):
    """Update user profile fields. Supports both full update (update) and incremental append (append)."""

    action: Literal["update", "append"] = Field(
        description="操作类型: update=全量更新字段，append=追加/更新字段（推荐用 append）"
    )
    # 基本信息字段
    name: str | None = Field(default=None, description="用户姓名")
    nickname: str | None = Field(default=None, description="用户称呼/昵称")
    timezone: str | None = Field(default=None, description="用户时区，如 Asia/Shanghai (GMT+8)")
    workspace: str | None = Field(default=None, description="用户工作目录路径")
    social_accounts: str | None = Field(default=None, description="社交账号，多个用换行分隔")

    # 外贸业务信息字段
    main_products: str | None = Field(
        default=None, description="主营产品（细化到关键词、产品名称或核心业务方向）"
    )
    company_name: str | None = Field(default=None, description="公司名称（全称或简称）")
    company_website: str | None = Field(default=None, description="公司网址（官网、独立站等）")
    company_scale: str | None = Field(
        default=None, description="公司规模（外贸SOHO/个体经营/小微团队/成长型/成熟公司）"
    )
    current_stage: str | None = Field(
        default=None,
        description="当前主要阶段/目标（刚起步/开拓新市场/拓展销售渠道/增加客户量/提升运营效率）",
    )
    main_industry: str | None = Field(default=None, description="主要行业（企业所属行业）")
    target_markets: str | None = Field(default=None, description="目标市场（全球地区、大洲/国家/范围）")
    platform_preference: str | None = Field(
        default=None, description="平台偏好（B2B平台、社媒平台、B2C、独立站等）"
    )
    style_preference: str | None = Field(
        default=None, description="风格偏好（语言风格、品牌风格、视觉风格等）"
    )
    business_strategy: str | None = Field(
        default=None, description="商务策略偏好（样品政策、报价策略、跟进方式、付款偏好等公司层面的商务习惯）"
    )

    # 通用信息字段
    background: str | None = Field(default=None, description="行业、角色、关注领域、工作内容")
    preferences: str | None = Field(default=None, description="语言、沟通风格、输出格式偏好")

    multi_field_append: bool | None = Field(
        default=True, description="多行字段是否追加到末尾而非替换（仅 append 生效）"
    )


# English param name → Chinese USER.md field name
_PARAM_TO_FIELD = {
    # 基本信息
    "name": "姓名",
    "nickname": "称呼",
    "timezone": "时区",
    "workspace": "工作区",
    "social_accounts": "社交账号",
    # 外贸业务信息
    "main_products": "主营产品",
    "company_name": "公司名称",
    "company_website": "公司网址",
    "company_scale": "公司规模",
    "current_stage": "当前主要阶段/目标",
    "main_industry": "主要行业",
    "target_markets": "目标市场",
    "platform_preference": "平台偏好",
    "style_preference": "风格偏好",
    "business_strategy": "商务策略偏好",
    # 通用信息
    "background": "背景",
    "preferences": "偏好",
}


class UpdateUserProfileTool(BaseTool):
    name: str = "update_user_profile"
    description: str = (
        "按字段更新用户画像。支持两种模式：\n"
        "- update: 全量更新字段（部分字段未传入时保留原值）\n"
        "- append: 追加/更新字段（推荐使用，未传入字段保留，多行字段智能追加）"
    )
    param_anno = UpdateUserProfileParams

    def __init__(self, user_id: str, workspace: "Workspace" = None, **kwargs):
        super().__init__(**kwargs)
        self._user_id = user_id
        self._ws = workspace or Workspace(user_id)

    async def __call__(self, params: UpdateUserProfileParams) -> str:
        try:
            # Determine operation type
            is_append = params.action == "append"

            # Map English param names → Chinese field names, skip None
            updates = {}
            for eng, cn in _PARAM_TO_FIELD.items():
                val = getattr(params, eng)
                if val is not None:
                    updates[cn] = val

            if is_append:
                updated = await _append_user_profile_fields(
                    self._user_id, updates, params.multi_field_append, ws=self._ws
                )
            else:
                updated = await _update_user_profile_fields(self._user_id, updates, ws=self._ws)

            if not updated:
                return json.dumps({"success": False, "message": "没有有效字段被更新。"}, ensure_ascii=False)

            # Read updated fields' full values from profile after write
            ws = self._ws
            raw = await ws_call(ws, "read_prompt", "USER") or ""
            body = _FRONTMATTER_RE.sub("", raw, count=1).strip()
            all_fields = _parse_user_fields(body)
            # Only the fields that were updated, with their complete post-update values
            fields = {k: all_fields.get(k, "") for k in updated}

            action_text = "追加" if is_append else "更新"
            fields_str = "、".join(updated)
            logger.info(f"{action_text.capitalize()}d user profile fields [{fields_str}] for {self._user_id}")
            return json.dumps(
                {
                    "success": True,
                    "action": action_text,
                    "fields": fields,
                    "message": f"已{action_text}字段：{fields_str}",
                },
                ensure_ascii=False,
            )
        except Exception as e:
            logger.error(f"Failed to update user profile: {e}")
            return json.dumps({"success": False, "message": f"操作失败: {str(e)}"}, ensure_ascii=False)


# --- Skill Manager Tool (read-only) ---


class SkillManagerParams(BaseModel):
    action: Literal["list", "get", "get_ref", "save", "delete", "ref_set", "ref_del"] = Field(
        description=(
            "操作类型: list=列出技能, get=加载技能文档, get_ref=获取参考文件, "
            "save=保存技能（立即生效）, delete=删除技能, "
            "ref_set=写入支撑文件, ref_del=删除支撑文件"
        )
    )
    name: str = Field(default="", description="技能名称（list 时可选，其他操作必填）")
    ref_filename: str = Field(
        default="", description="参考文件路径（get_ref/ref_set/ref_del 必填，如 references/xxx.md）"
    )
    # --- save 专用字段 ---
    description: str = Field(default="", description="技能描述（save 必填）")
    triggers: list[str] = Field(default=[], description="触发词列表（save 必填）")
    instructions: str = Field(default="", description="技能执行步骤（save 必填）")
    required_tools: list[str] = Field(default=[], description="所需工具列表（save 可选）")
    tags: list[str] = Field(default=[], description="分类标签（save 可选）")
    source: str = Field(
        default="user_defined", description="技能来源: user_defined 或 extracted（save 可选）"
    )
    # --- ref_set 专用字段 ---
    ref_content: str = Field(default="", description="支撑文件内容（ref_set 必填）")


class SkillManagerTool(BaseTool):
    name: str = "skill_manager"
    description: str = (
        "技能管理工具。\n"
        "- list: 列出技能\n"
        "- get: 加载技能文档和参考文件列表\n"
        "- get_ref: 获取参考文件内容\n"
        "- save: 保存/创建技能（立即生效）\n"
        "- delete: 删除技能\n"
        "- ref_set: 写入支撑文件\n"
        "- ref_del: 删除支撑文件"
    )
    param_anno = SkillManagerParams

    def __init__(
        self,
        user_id: str,
        skill_manager: SkillManager,
        workspace: "Workspace" = None,
        session_id: str = "",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._user_id = user_id
        self._session_id = session_id
        self._skill_manager = skill_manager
        self._ws = workspace or Workspace(user_id)
        self._got_skills: set = set()

    async def __call__(self, params: SkillManagerParams) -> str:
        try:
            if params.action == "list":
                return self._action_list(params.name)
            elif params.action == "get":
                return await self._action_get(params.name)
            elif params.action == "get_ref":
                return await self._action_get_ref(params.name, params.ref_filename)
            elif params.action == "save":
                return self._action_save(params)
            elif params.action == "delete":
                return self._action_delete(params.name)
            elif params.action == "ref_set":
                return await self._action_ref_set(params.name, params.ref_filename, params.ref_content)
            elif params.action == "ref_del":
                return await self._action_ref_del(params.name, params.ref_filename)
            else:
                return f"未知操作: {params.action}"
        except Exception as e:
            logger.error(f"skill_manager {params.action} failed: {e}")
            return f"操作失败: {str(e)}"

    def _action_list(self, parent: str = "") -> str:
        all_skills = self._skill_manager.get_all_skills(self._user_id)
        if not all_skills:
            return "当前没有已确认的技能。"

        if parent:
            prefix = parent.rstrip("/") + "/"
            children = {
                k: v for k, v in all_skills.items() if k.startswith(prefix) and "/" not in k[len(prefix) :]
            }
            if not children:
                return f"技能 '{parent}' 没有子技能。"
            lines = [f"- **{s.name}**: {s.description}" for s in children.values()]
            return f"'{parent}' 的子技能 ({len(children)}):\n" + "\n".join(lines)

        top_level = {k: v for k, v in all_skills.items() if "/" not in k}
        if not top_level:
            return "当前没有已确认的技能。"
        lines = []
        for s in top_level.values():
            triggers = ", ".join(s.triggers) if s.triggers else "无"
            lines.append(
                f"- **{s.name}** (v{s.version}, 使用{s.usage_count}次): {s.description} [触发词: {triggers}]"
            )
        return f"已确认技能 ({len(top_level)}):\n" + "\n".join(lines)

    async def _action_get(self, name: str) -> str:
        if not name:
            return "请提供技能名称。"
        if name in self._got_skills:
            return f"技能 '{name}' 已加载，无需重复获取。"

        raw = await ws_call(self._ws, "skill_get", name)
        if not raw:
            return f"技能 '{name}' 不存在。"
        self._got_skills.add(name)

        result = f"技能文档 `{name}/SKILL.md`:\n\n```markdown\n{raw}\n```"
        refs = await ws_call(self._ws, "skill_refs_get", name)
        if refs:
            result += f"\n\n可用支撑文件 ({len(refs)}):"
            for filename, content in refs.items():
                result += f"\n- `{filename}` ({len(content)}字符) → skill_manager(action='get_ref', name='{name}', ref_filename='{filename}')"
        return result

    async def _action_get_ref(self, name: str, ref_filename: str) -> str:
        if not name or not ref_filename:
            return "请提供技能名称和参考文件路径。"
        refs = await ws_call(self._ws, "skill_refs_get", name)
        if ref_filename not in refs:
            return f"参考文件 '{ref_filename}' 不存在于技能 '{name}' 中。"
        return f"`{name}/{ref_filename}`:\n\n```\n{refs[ref_filename]}\n```"

    def _action_save(self, params: SkillManagerParams) -> str:
        if not params.name or not params.description or not params.instructions:
            return "save 操作需要 name、description、instructions 三个字段。"
        from core.skills import Skill

        skill = Skill(
            name=params.name,
            description=params.description,
            triggers=params.triggers,
            instructions=params.instructions,
            required_tools=params.required_tools,
            tags=params.tags,
            source=params.source if params.source in ("user_defined", "extracted") else "user_defined",
            extracted_from=self._session_id,
        )
        self._skill_manager.save_user_skill(self._user_id, skill)
        logger.info(f"Saved skill '{params.name}' for user {self._user_id}")
        return f"技能 '{params.name}' 已保存并立即生效。"

    def _action_delete(self, name: str) -> str:
        if not name:
            return "请提供技能名称。"
        ok = self._skill_manager.delete_user_skill(self._user_id, name)
        return f"技能 '{name}' 已删除。" if ok else f"技能 '{name}' 不存在。"

    async def _action_ref_set(self, name: str, ref_filename: str, ref_content: str) -> str:
        if not name or not ref_filename or not ref_content:
            return "ref_set 操作需要 name、ref_filename、ref_content 三个字段。"
        await ws_call(self._ws, "skill_ref_set", name, ref_filename, ref_content)
        logger.info(f"Set ref '{ref_filename}' for skill '{name}'")
        return f"已写入参考文件 '{ref_filename}' 到技能 '{name}'。"

    async def _action_ref_del(self, name: str, ref_filename: str) -> str:
        if not name or not ref_filename:
            return "ref_del 操作需要 name 和 ref_filename 字段。"
        ok = await ws_call(self._ws, "skill_ref_del", name, ref_filename)
        return f"已删除参考文件 '{ref_filename}'。" if ok else f"参考文件 '{ref_filename}' 不存在。"


# --- Memory Tool (index + per-type files) ---

MEMORY_TYPES = ("user", "feedback", "project", "reference")
_INDEX_LINE_RE = re.compile(
    r"^-\s*\[(?P<type>user|feedback|project|reference)\]\s*"
    r"(?P<title>.+?)\s*\((?P<date>\d{4}-\d{2}-\d{2})\)\s*"
    r"(?:—\s*(?P<summary>.+))?$"
)
_GET_BODY_MAX_CHARS = 800  # Hard cap on returned detail body (protects token budget)
_SUMMARY_MAX_CHARS = 40  # Soft cap on index-line summary length


class UpdateMemoryParams(BaseModel):
    action: Literal["read", "get", "append", "rewrite", "rebuild_index"] = Field(
        description=(
            "操作类型:\n"
            "- read: 返回 MEMORY.md 索引（system prompt 已常驻，通常无需显式调用）\n"
            "- get: 按 title 加载某条记忆的完整正文\n"
            "- append: 写入一条新记忆（到对应 type 文件 + 索引加一行）\n"
            "- rewrite: 重写某一个 type 文件（整理/精简时用）\n"
            "- rebuild_index: 从 4 个 type 文件扫描重建索引（灾难恢复）"
        )
    )
    title: str | None = Field(
        default=None,
        description="记忆条目标题（get / append 必填，同一 type 下应唯一）",
    )
    type: Literal["user", "feedback", "project", "reference"] | None = Field(
        default=None,
        description=(
            "记忆类型: user=用户画像外延, feedback=协作偏好/纠正/确认, "
            "project=项目动向, reference=外部系统指针。"
            "append/rewrite 必填，get 可选（留空时从索引推导）"
        ),
    )
    content: str | None = Field(
        default=None,
        description="正文（append 时为单条正文，rewrite 时为该 type 文件的全部内容）",
    )
    summary: str | None = Field(
        default=None,
        description=f"索引摘要（仅 append，一行≤{_SUMMARY_MAX_CHARS}字；留空时自动截 content 首行）",
    )


class UpdateMemoryTool(BaseTool):
    name: str = "update_memory"
    description: str = (
        "读写长期记忆。MEMORY.md 是索引（system prompt 已加载），详情按需用 get 取。\n"
        "- read: 返回索引原文（调试用；对话中通常直接看 system prompt）\n"
        "- get(title, type?): 加载一条记忆的完整正文，body 超 800 字会截断\n"
        "- append(type, title, content, summary?): 写入新记忆，索引同步更新\n"
        "- rewrite(type, content): 整段重写某一 type 文件，索引自动重建\n"
        "- rebuild_index: 从 4 个 type 文件扫出标题重建索引（数据不一致时修复）"
    )
    param_anno = UpdateMemoryParams

    def __init__(self, user_id: str, workspace: "Workspace" = None, **kwargs):
        super().__init__(**kwargs)
        self._user_id = user_id
        self._ws = workspace or Workspace(user_id)
        self._got_titles: set[str] = set()  # dedup cache for `get` within a session

    async def __call__(self, params: UpdateMemoryParams) -> str:
        try:
            action = params.action
            if action == "read":
                return await self._action_read()
            if action == "get":
                if not params.title:
                    return "get 操作需要 title。"
                return await self._action_get(params.title, params.type)
            if action == "append":
                if not params.type or not params.title or not params.content:
                    return "append 需要 type + title + content。"
                return await self._action_append(params.type, params.title, params.content, params.summary)
            if action == "rewrite":
                if not params.type or not params.content:
                    return "rewrite 需要 type + content。"
                return await self._action_rewrite(params.type, params.content)
            if action == "rebuild_index":
                return await self._action_rebuild_index()
            return f"未知操作: {action}"
        except Exception as e:
            logger.error(f"update_memory {params.action} failed: {e}")
            return f"操作失败: {str(e)}"

    # ── Shared I/O helpers ─────────────────────────────────

    async def _read_file(self, name: str) -> tuple[str, str]:
        """Return (frontmatter, body) from a prompt file. Missing file -> ('', '')."""
        raw = await ws_call(self._ws, "read_prompt", name) or ""
        if not raw.strip():
            return "", ""
        m = _FRONTMATTER_RE.match(raw)
        frontmatter = m.group(1) if m else ""
        body = _FRONTMATTER_RE.sub("", raw, count=1).strip()
        return frontmatter, body

    async def _write_file(self, name: str, body: str, default_description: str = "") -> None:
        """Write body (with existing or default frontmatter) back to a prompt file."""
        frontmatter, _ = await self._read_file(name)
        if not frontmatter:
            frontmatter = f"---\nname: {name}\ndescription: {default_description}\n---\n"
        await ws_call(self._ws, "write_prompt", name, frontmatter + "\n" + body.rstrip() + "\n")

    @staticmethod
    def _parse_entries(body: str) -> list[tuple[str, str, str]]:
        """Parse a typed file body into [(title, date, entry_body), ...].

        Entry format: ``## {title} ({YYYY-MM-DD})\\n{body}\\n`` separated by next ``## ``.
        """
        if not body.strip():
            return []
        entries = []
        pattern = re.compile(r"^## (.+?) \((\d{4}-\d{2}-\d{2})\)\s*$", re.MULTILINE)
        matches = list(pattern.finditer(body))
        for i, m in enumerate(matches):
            title = m.group(1).strip()
            date = m.group(2)
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            entry_body = body[start:end].strip()
            entries.append((title, date, entry_body))
        return entries

    # ── Actions ──────────────────────────────────────────────

    async def _action_read(self) -> str:
        _, body = await self._read_file("MEMORY")
        if not body:
            return "记忆索引为空。"
        return body

    async def _action_get(self, title: str, mem_type: str | None) -> str:
        # Dedup: same title already fetched this session
        if title in self._got_titles:
            return f"记忆 '{title}' 已在本会话中加载过，请在历史的 tool_result 里回看。"

        # Resolve type from index if not provided
        if mem_type is None:
            mem_type = await self._lookup_type_from_index(title)
            if mem_type is None:
                return f"在索引里找不到 title='{title}' 的记忆。先 read 索引确认名称。"

        if mem_type not in MEMORY_TYPES:
            return f"未知 type: {mem_type}，合法值: {MEMORY_TYPES}"

        _, body = await self._read_file(f"MEMORY_{mem_type}")
        entries = self._parse_entries(body)
        for t, date, entry_body in entries:
            if t == title:
                truncated = entry_body
                if len(truncated) > _GET_BODY_MAX_CHARS:
                    truncated = (
                        truncated[:_GET_BODY_MAX_CHARS] + f"\n\n... (已截断，完整内容见 MEMORY_{mem_type}.md)"
                    )
                self._got_titles.add(title)
                return f"## {title} ({date}) [type={mem_type}]\n\n{truncated}"

        return f"MEMORY_{mem_type}.md 里找不到 section '## {title}'。索引可能过期，建议 rebuild_index。"

    async def _action_append(self, mem_type: str, title: str, content: str, summary: str | None) -> str:
        if mem_type not in MEMORY_TYPES:
            return f"未知 type: {mem_type}，合法值: {MEMORY_TYPES}"
        title = title.strip()
        content = content.strip()
        if not title or not content:
            return "title 和 content 不能为空。"

        date = datetime.now().strftime("%Y-%m-%d")
        summary = (summary or content.splitlines()[0]).strip()
        if len(summary) > _SUMMARY_MAX_CHARS:
            summary = summary[:_SUMMARY_MAX_CHARS].rstrip() + "…"

        # 1) Append to typed file (dedup: if title already exists, reject)
        _, typed_body = await self._read_file(f"MEMORY_{mem_type}")
        existing = {t for t, _, _ in self._parse_entries(typed_body)}
        if title in existing:
            return (
                f"MEMORY_{mem_type}.md 已存在同名条目 '{title}'。"
                "如需更新，先 rewrite 整段；如需追加到同一主题，请换个 title。"
            )
        new_entry = f"## {title} ({date})\n\n{content}\n"
        new_typed_body = (typed_body + "\n\n" + new_entry).strip() if typed_body else new_entry
        await self._write_file(
            f"MEMORY_{mem_type}",
            new_typed_body,
            default_description=f"{mem_type} 类型的长期记忆条目",
        )

        # 2) Append a line to MEMORY.md index
        _, index_body = await self._read_file("MEMORY")
        index_line = f"- [{mem_type}] {title} ({date}) — {summary}"
        new_index_body = (index_body + "\n" + index_line).strip() if index_body else index_line
        await self._write_file("MEMORY", new_index_body, default_description="长期记忆索引")

        logger.info(f"Appended memory [{mem_type}] '{title}' for user {self._user_id}")
        return f"已写入 MEMORY_{mem_type}.md 并更新索引：{index_line}"

    async def _action_rewrite(self, mem_type: str, content: str) -> str:
        if mem_type not in MEMORY_TYPES:
            return f"未知 type: {mem_type}，合法值: {MEMORY_TYPES}"
        content = content.strip()
        if not content:
            return "content 不能为空。"

        await self._write_file(
            f"MEMORY_{mem_type}",
            content,
            default_description=f"{mem_type} 类型的长期记忆条目",
        )
        await self._action_rebuild_index(silent=True)

        logger.info(f"Rewrote MEMORY_{mem_type}.md for user {self._user_id}")
        return f"MEMORY_{mem_type}.md 已重写，索引已同步。"

    async def _action_rebuild_index(self, silent: bool = False) -> str:
        """Scan all 4 typed files and regenerate MEMORY.md index from scratch."""
        lines: list[str] = []
        for mem_type in MEMORY_TYPES:
            _, typed_body = await self._read_file(f"MEMORY_{mem_type}")
            for title, date, entry_body in self._parse_entries(typed_body):
                summary = entry_body.splitlines()[0].strip() if entry_body else ""
                if len(summary) > _SUMMARY_MAX_CHARS:
                    summary = summary[:_SUMMARY_MAX_CHARS].rstrip() + "…"
                lines.append(f"- [{mem_type}] {title} ({date}) — {summary}")

        body = "\n".join(lines) if lines else ""
        await self._write_file("MEMORY", body, default_description="长期记忆索引")
        if silent:
            return ""
        logger.info(f"Rebuilt memory index for user {self._user_id}: {len(lines)} entries")
        return f"索引已重建，共 {len(lines)} 条记忆。"

    async def _lookup_type_from_index(self, title: str) -> str | None:
        """Scan MEMORY.md index for a line matching the given title; return its type."""
        _, body = await self._read_file("MEMORY")
        for line in body.splitlines():
            m = _INDEX_LINE_RE.match(line.strip())
            if m and m.group("title").strip() == title:
                return m.group("type")
        return None


# --- User Profile helpers ---

_FRONTMATTER_RE = re.compile(r"^(---\s*\n.*?\n---\s*\n)", re.DOTALL)

_BASIC_SINGLE = ["姓名", "称呼", "时区", "工作区"]
_BASIC_MULTI = ["社交账号"]
_TRADE_SINGLE = [
    "主营产品",
    "公司名称",
    "公司网址",
    "公司规模",
    "当前主要阶段/目标",
    "主要行业",
    "目标市场",
    "平台偏好",
    "风格偏好",
    "商务策略偏好",
]
_GENERAL_MULTI = ["背景", "偏好"]

_SINGLE_FIELDS = _BASIC_SINGLE + _TRADE_SINGLE
_MULTI_FIELDS = _BASIC_MULTI + _GENERAL_MULTI
_ALL_FIELDS = _SINGLE_FIELDS + _MULTI_FIELDS

# 逗号分隔列表字段：append 时追加新值到已有列表，自动去重
_LIST_FIELDS = {"主营产品", "目标市场", "平台偏好"}


def _parse_user_fields(body: str) -> dict[str, str]:
    """Parse USER.md body into {field_name: value} dict."""
    fields: dict[str, str] = {f: "" for f in _ALL_FIELDS}
    if not body.strip():
        return fields

    lines = body.split("\n")
    current_field: str | None = None
    current_lines: list[str] = []

    for line in lines:
        # Skip section headers and hint lines
        stripped = line.strip()
        if stripped.startswith("## ") or (stripped.startswith("（") and stripped.endswith("）")):
            # Save previous field before skipping
            if current_field is not None:
                fields[current_field] = "\n".join(current_lines).strip()
                current_field = None
                current_lines = []
            continue

        # Check if this line starts a new field
        matched = None
        for f in _ALL_FIELDS:
            if line.startswith(f"{f}："):
                matched = f
                break

        if matched:
            # Save previous field
            if current_field is not None:
                fields[current_field] = "\n".join(current_lines).strip()

            current_field = matched
            if matched in _SINGLE_FIELDS:
                # Single-line: value is on the same line after "字段名："
                value = line[len(f"{matched}：") :].strip()
                current_lines = [value] if value else []
            else:
                # Multi-line: value starts from next line
                current_lines = []
        else:
            if current_field is not None:
                current_lines.append(line)

    # Save last field
    if current_field is not None:
        fields[current_field] = "\n".join(current_lines).strip()

    return fields


async def _append_user_profile_fields(
    user_id: str, updates: dict[str, str], multi_field_append: bool = True, ws=None
) -> list[str]:
    """Append/Update fields to USER.md. Preserves existing fields not being updated."""
    from datetime import datetime

    ws = ws or Workspace(user_id)
    raw = await ws_call(ws, "read_prompt", "USER")
    if not raw:
        return []

    # Filter to valid fields only
    valid_updates = {k: v.strip() for k, v in updates.items() if k in _ALL_FIELDS}
    if not valid_updates:
        return []

    # Parse existing fields
    body = _FRONTMATTER_RE.sub("", raw, count=1).strip()
    fields = _parse_user_fields(body)

    # Determine multi-line fields that need special handling
    multi_line_fields = {
        "background": "背景",
        "preferences": "偏好",
        "social_accounts": "社交账号",
    }

    # Process each update
    for key, value in valid_updates.items():
        value = value.strip()
        if not value:
            continue  # Skip empty values

        if key in _LIST_FIELDS and multi_field_append:
            # Comma-separated list field: deduplicated append
            existing = fields.get(key, "").strip()
            if existing:
                existing_items = [i.strip() for i in re.split(r"[,，、]", existing) if i.strip()]
                new_items = [i.strip() for i in re.split(r"[,，、]", value) if i.strip()]
                seen = set(existing_items)
                for item in new_items:
                    if item not in seen:
                        existing_items.append(item)
                        seen.add(item)
                fields[key] = "、".join(existing_items)
            else:
                fields[key] = value
        elif key in multi_line_fields and multi_field_append:
            # Multi-line field: append with separator and timestamp
            existing = fields.get(key, "").strip()
            if existing:
                timestamp = datetime.now().strftime("[%Y-%m-%d %H:%M]")
                fields[key] = existing + f"\n{timestamp} {value}"
            else:
                timestamp = datetime.now().strftime("[%Y-%m-%d %H:%M]")
                fields[key] = f"{timestamp} {value}"
        else:
            # Single-line field: update value
            fields[key] = value

    # Serialize back
    new_body = _serialize_user_fields(fields)

    # Preserve frontmatter and write back
    m = _FRONTMATTER_RE.match(raw)
    frontmatter = m.group(1) if m else ""
    await ws_call(ws, "write_prompt", "USER", frontmatter + "\n" + new_body + "\n")

    return list(valid_updates.keys())


def _serialize_user_fields(fields: dict[str, str]) -> str:
    """Serialize {field_name: value} dict back to USER.md body text with section headers."""
    parts: list[str] = []

    # 基本信息
    parts.append("## 基本信息")
    for f in _BASIC_SINGLE:
        value = fields.get(f, "")
        parts.append(f"{f}： {value}" if value else f"{f}：")
    for f in _BASIC_MULTI:
        value = fields.get(f, "")
        parts.append("")
        parts.append(f"{f}：")
        if value:
            parts.append(value)

    # 外贸业务信息
    parts.append("")
    parts.append("")
    parts.append("## 外贸业务信息")
    for f in _TRADE_SINGLE:
        value = fields.get(f, "")
        parts.append(f"{f}： {value}" if value else f"{f}：")

    # 通用信息
    parts.append("")
    parts.append("")
    parts.append("## 通用信息")
    for f in _GENERAL_MULTI:
        value = fields.get(f, "")
        parts.append(f"{f}：")
        if value:
            parts.append(value)
        parts.append("")

    return "\n".join(parts)


async def _update_user_profile_fields(user_id: str, updates: dict[str, str], ws=None) -> list[str]:
    """Update specific fields in USER.md. Returns list of updated field names."""
    ws = ws or Workspace(user_id)
    raw = await ws_call(ws, "read_prompt", "USER")
    if not raw:
        return []

    # Filter to valid fields only
    valid_updates = {k: v.strip() for k, v in updates.items() if k in _ALL_FIELDS}
    if not valid_updates:
        return []

    # Parse → merge → serialize
    body = _FRONTMATTER_RE.sub("", raw, count=1).strip()
    fields = _parse_user_fields(body)
    for key, value in valid_updates.items():
        fields[key] = value
    new_body = _serialize_user_fields(fields)

    # Preserve frontmatter and write back
    m = _FRONTMATTER_RE.match(raw)
    frontmatter = m.group(1) if m else ""
    await ws_call(ws, "write_prompt", "USER", frontmatter + "\n" + new_body + "\n")

    return list(valid_updates.keys())


async def get_user_profile(user_id: str, ws=None) -> str:
    """Read the body of the user's USER.md (everything after frontmatter)."""
    ws = ws or Workspace(user_id)
    raw = await ws_call(ws, "read_prompt", "USER")
    if not raw:
        return ""

    return _FRONTMATTER_RE.sub("", raw, count=1).strip()


# --- Schedule Manager Tool ---


class ScheduleManagerParams(BaseModel):
    """Manage scheduled tasks."""

    action: Literal["create", "list", "delete"] = Field(
        description="操作类型: create=创建定时任务, list=列出所有任务, delete=删除任务"
    )
    name: str | None = Field(default=None, description="任务名称（create 时必填）")
    skill: str | None = Field(default=None, description="关联的技能名称（可选）")
    skill_desc: str | None = Field(default=None, description="技能简短描述（可选）")
    prompt: str | None = Field(default=None, description="定时执行时发送给 agent 的提示词（create 时必填）")
    hour: int | None = Field(default=8, description="执行时间-小时（0-23，默认8点）")
    minute: int | None = Field(default=0, description="执行时间-分钟（0-59，默认0分）")
    repeat: str | None = Field(default="daily", description="重复方式: daily=每天, weekly=每周, once=仅一次")
    task_id: str | None = Field(default=None, description="任务ID（delete 时必填）")


class ScheduleManagerTool(BaseTool):
    name: str = "schedule_manager"
    description: str = (
        "创建、查看、删除定时任务。当用户要求定期/每天/每周自动执行某件事时使用此工具。\n"
        "- create: 创建定时任务（需要 name, prompt, hour, minute）\n"
        "- list: 列出当前所有定时任务\n"
        "- delete: 删除指定任务（需要 task_id）\n"
        "任务创建后会推送到用户前端，由前端定时触发执行。"
    )
    param_anno = ScheduleManagerParams

    def __init__(self, user_id: str, workspace: "Workspace" = None, **kwargs):
        super().__init__(**kwargs)
        self._user_id = user_id
        self._ws = workspace or Workspace(user_id)

    async def __call__(self, params: ScheduleManagerParams) -> str:
        import time

        if params.action == "create":
            if not params.name or not params.prompt:
                return "创建任务需要 name 和 prompt 参数。"
            task = {
                "id": f"t_{int(time.time())}",
                "name": params.name,
                "skill": params.skill or "",
                "skill_desc": params.skill_desc or "",
                "prompt": params.prompt,
                "hour": params.hour if params.hour is not None else 8,
                "minute": params.minute if params.minute is not None else 0,
                "repeat": params.repeat or "daily",
                "enabled": True,
            }
            # Return JSON so agent.py can detect and emit code 7
            return json.dumps({"action": "create", "task": task}, ensure_ascii=False)

        elif params.action == "list":
            return json.dumps({"action": "list"}, ensure_ascii=False)

        elif params.action == "delete":
            if not params.task_id:
                return "删除任务需要 task_id 参数。"
            return json.dumps({"action": "delete", "task_id": params.task_id}, ensure_ascii=False)

        return f"未知操作: {params.action}"


# --- Parent History Tool (for sub-agents) ---


class ParentHistoryParams(BaseModel):
    """Query the parent agent's conversation history (sub-agent use only)."""

    action: Literal["tail", "full", "search", "last_user"] = Field(
        description=(
            "tail: 最近 N 条消息；"
            "full: 完整历史（会按长度截断）；"
            "search: 用 keyword 做不区分大小写的子串过滤；"
            "last_user: 仅返回最近一条用户消息"
        ),
    )
    n: int = Field(
        default=5,
        description="action=tail 时返回的消息数（默认 5，上限 30）",
    )
    keyword: str = Field(
        default="",
        description="action=search 时的关键词",
    )


_PARENT_HIST_MAX_CHARS = 15000
_PARENT_HIST_MAX_MSGS = 50
_PARENT_HIST_PER_MSG_CHARS = 800


class ParentHistoryTool(BaseTool):
    """Let a sub-agent read its parent agent's conversation history on demand.

    Only instantiated when a sub-agent is spawned by a parent (via
    ``SubAgentTool`` or Phase 2.5 handoff). Sub-agents invoked directly
    through ``POST /api/sub-agent/{type}/chat`` do not have this tool.
    """

    name: str = "parent_history"
    description: str = (
        "按需查看主 agent 的对话历史。主 agent 启动子任务前**未必**会把所有背景写进 task/context，"
        "当你需要原始上下文（用户说过什么、之前调用过什么工具、已贴过的 URL/代码/错误信息等）时使用此工具。\n"
        "- action=tail, n=5：最近 5 条消息\n"
        "- action=full：完整历史（会按长度截断）\n"
        "- action=search, keyword=xxx：找包含 xxx 的消息\n"
        "- action=last_user：最近一条用户消息（适合快速确认当前意图）"
    )
    param_anno = ParentHistoryParams

    def __init__(self, parent_history_fn, **kwargs):
        super().__init__(**kwargs)
        # Callable returning the *live* parent history list; must not be None.
        self._fn = parent_history_fn

    async def __call__(self, params: ParentHistoryParams) -> str:
        try:
            history = list(self._fn() or [])
        except Exception as e:
            logger.warning(f"ParentHistoryTool fetch failed: {e}")
            return f"无法读取主 agent 历史: {e}"

        if not history:
            return "主 agent 历史为空。"

        if params.action == "tail":
            n = max(1, min(int(params.n or 5), 30))
            return self._format(history[-n:])

        if params.action == "full":
            return self._format(history)

        if params.action == "search":
            kw = (params.keyword or "").strip().lower()
            if not kw:
                return "search 需要提供 keyword。"
            matches = [m for m in history if kw in str(m.get("content", "")).lower()]
            if not matches:
                return f"未找到包含 {kw!r} 的消息。"
            return self._format(matches)

        if params.action == "last_user":
            for m in reversed(history):
                if m.get("role") == "user":
                    return str(m.get("content", ""))[:_PARENT_HIST_MAX_CHARS]
            return "主 agent 历史中没有用户消息。"

        return f"未知 action: {params.action}"

    @staticmethod
    def _format(messages: list) -> str:
        if not messages:
            return "(无匹配消息)"
        # Keep only the most recent N to bound token cost
        tail = messages[-_PARENT_HIST_MAX_MSGS:]
        lines = []
        for m in tail:
            role = m.get("role", "unknown")
            content = m.get("content", "")
            if content is None:
                content = "[工具调用]"
            text = str(content)[:_PARENT_HIST_PER_MSG_CHARS]
            lines.append(f"[{role}] {text}")
        result = "\n\n".join(lines)
        if len(result) > _PARENT_HIST_MAX_CHARS:
            result = result[:_PARENT_HIST_MAX_CHARS] + "\n... [history truncated]"
        header = f"主 agent 历史（共 {len(messages)} 条，展示 {len(tail)} 条）:\n\n"
        return header + result


# --- Sub-Agent Tool ---


class SubAgentParams(BaseModel):
    """Parameters for invoking a sub-agent."""

    task: str = Field(description="子代理要执行的任务描述")
    agent_type: str = Field(
        description="子代理类型（对应 configs/agents/ 下的 .md 文件名）",
    )
    context: str | None = Field(
        default=None,
        description="传递给子代理的额外上下文信息",
    )


class SubAgentTool(BaseTool):
    """Invoke a sub-agent to execute an independent task.

    SubAgentRunner runs inside __call__, collecting text output for SDK return.
    Sub-agent's tool events (code 4/5/9/10) are stored in _sub_events for
    _stream_sdk_events to flush before emitting the tool result.
    """

    name: str = "sub_agent"
    description: str = "调用子代理执行独立任务。"
    param_anno = SubAgentParams

    def __init__(self, user_id: str = "", workspace=None, parent_history_fn=None, **kwargs):
        super().__init__(**kwargs)
        self._user_id = user_id
        self._workspace = workspace
        # Callable returning the live parent-agent history. When set, the
        # sub-agent will get a `parent_history` tool it can use on demand.
        self._parent_history_fn = parent_history_fn
        self._sub_events: list[dict] = []
        # Dynamically build description from available agent types
        try:
            from core.sub_agent import list_agent_types

            agents = list_agent_types()
            if agents:
                lines = "\n".join(f"- {a.name}: {a.description}" for a in agents)
                self.description = f"调用子代理执行独立任务。可用类型：\n{lines}"
        except Exception:
            pass

    async def __call__(self, params: SubAgentParams) -> str:
        """Run sub-agent, collect text output, buffer tool events."""
        from core.sub_agent import SubAgentRunner

        self._sub_events.clear()

        try:
            runner = SubAgentRunner(
                agent_type=params.agent_type,
                user_id=self._user_id,
                session_id=f"from_main_{uuid4().hex[:8]}",
                workspace=self._workspace,
                parent_history_fn=self._parent_history_fn,
            )
        except ValueError as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

        final_text = ""
        try:
            async for event in runner.run_stream(params.task, params.context or ""):
                code = event.get("code")
                content = event.get("content", "")

                if code == 2:
                    final_text += content
                elif code in (4, 5, 9, 10):
                    self._sub_events.append(event)

        except Exception as e:
            logger.error(f"SubAgentTool error: {e}")
            return json.dumps({"error": str(e)}, ensure_ascii=False)

        return final_text[:4000] if final_text else "子代理执行完成"
