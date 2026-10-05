#!/usr/bin/env python3
"""survey26: the official command-line tool of the GOSIM 2026 Agentic Observer Hackathon.

Everything a contestant can do on https://create.gosim.org/survey26/ (team, profile,
team variables, project versions, evaluations, results, final version, leaderboard),
from a terminal or a coding agent. Python 3.9+, standard library only.

Authentication: create a personal API token on the website (Profile -> Personal API
tokens), then either export SURVEY26_TOKEN=s26_... or run `survey26 login --token-stdin`.
A token acts as you, with exactly your website permissions, quotas and limits.

Every command accepts --json: one JSON object on stdout,
  {"ok": true, "command": "...", "data": ...}  or
  {"ok": false, "command": "...", "error": {"code": "...", "message": "...", "exit_code": N}}
Exit codes: 0 ok, 1 refused by the server, 2 usage / confirmation needed, 3 authentication,
4 not found, 5 rate limited, 6 network or server unavailable, 7 wait timed out,
8 a daily or team limit was reached, 9 the awaited preparation or evaluation failed.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import http.client
import io
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

__version__ = "1.7.0"

DEFAULT_API = "https://vdiemcofukuxglqsmlyz.supabase.co/functions/v1/survey26-cli"
SITE = "https://create.gosim.org/survey26/platform"
TOKEN_RE = re.compile(r"^s26_[0-9a-f]{64}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
ACTIVE = ("queued", "running")
FINISHED_RUN = ("scored", "failed", "cancelled")
REVISION_PENDING = ("queued", "preparing")
SELF_CHECK_RUNS = 3

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_AUTH, EXIT_NOT_FOUND = 0, 1, 2, 3, 4
EXIT_RATE, EXIT_UNAVAILABLE, EXIT_TIMEOUT, EXIT_LIMIT, EXIT_FAILED = 5, 6, 7, 8, 9

AUTH_CODES = {"invalid_token", "cli_tokens_disabled", "account_banned", "account_unavailable", "login_required",
              "no_token", "banned", "not_authenticated"}
LIMIT_CODES = {"daily_limit", "repeat_daily_limit", "preparation_limit", "preparation_daily_limit", "upload_limit",
               "batch_already_active", "team_variable_limit", "full", "team_limit_reached", "no_codes_left", "uid_daily_limit"}
NOT_FOUND_CODES = {"revision_not_found", "run_not_found", "result_not_ready", "project_not_ready", "upload_not_found",
                   "not_found", "token_not_found", "diagnostics_not_found", "invitation_not_found", "uid_not_found",
                   "uid_unavailable", "request_not_found", "user_not_found", "repository_not_found",
                   "source_ref_not_found", "source_subdir_not_found"}
UNAVAILABLE_CODES = {"network_error", "gateway_unavailable", "portal_unavailable", "session_unavailable",
                     "source_snapshot_unavailable", "artifact_service_unavailable", "request_failed"}

# The website's own wording for each error code (en, zh), so the CLI says what the page says.
MESSAGES = {
    # Command-line specific
    "no_token": ("No API token. Create one on the website (Profile -> Personal API tokens), then export SURVEY26_TOKEN or run `survey26 login --token-stdin`.",
                 "没有 API 令牌。请在网站「个人资料 → 个人 API 令牌」中创建，然后设置 SURVEY26_TOKEN 环境变量或运行 `survey26 login --token-stdin`。"),
    "invalid_token": ("This API token is not valid (mistyped, revoked, or deleted). Create a new one on your profile page.",
                      "API 令牌无效（输入错误、已撤销或已删除）。请在个人资料页重新创建。"),
    "cli_tokens_disabled": ("API tokens are not available right now.", "API 令牌功能暂未开放。"),
    "account_banned": ("This account is suspended.", "该账号已被停用。"),
    "account_unavailable": ("This account is suspended.", "该账号已被停用。"),
    "rate_limited": ("Too many requests. Wait a minute and try again.", "请求过于频繁，请稍等一分钟再试。"),
    "network_error": ("Could not reach the server. Check your connection and try again.", "无法连接服务器，请检查网络后重试。"),
    "gateway_unavailable": ("The server is temporarily unavailable. Try again shortly.", "服务器暂时不可用，请稍后再试。"),
    "session_unavailable": ("The server is temporarily unavailable. Try again shortly.", "服务器暂时不可用，请稍后再试。"),
    "action_not_available": ("This action is not available with an API token.", "该操作不能通过 API 令牌使用。"),
    "confirmation_required": ("This action needs confirmation: run it again with --yes.", "该操作需要确认：请加上 --yes 重新运行。"),
    "wait_timeout": ("Still not finished when the wait timed out. Run the wait command again later.", "等待超时，任务仍未结束。请稍后再次运行等待命令。"),
    "ambiguous_id": ("This ID prefix matches more than one item. Use more characters.", "该 ID 前缀匹配到多个对象，请多输入几位。"),
    "no_open_phase": ("No evaluation phase is open.", "当前没有开放的评测赛程。"),
    "revision_not_reviewable": ("This version is not ready for confirmation (it must be ready for review).", "该版本尚不能确认（需处于“等待确认”状态）。"),
    "no_team_version": ("No final version yet. Confirm a version and evaluate it, or choose one.", "还没有最终版本。请先确认并评测一个版本，或在下方选择。"),
    "file_not_found": ("File not found.", "找不到文件。"),
    "download_failed": ("The download failed. Try again.", "下载失败，请重试。"),
    "invalid_api_response": ("Unexpected answer from the server.", "服务器返回了无法识别的内容。"),
    # observer-portal (competition workspace)
    "team_required": ("Join or create a team first.", "请先加入或创建队伍。"),
    "stale_approval": ("The version changed. Reopen the review before confirming.", "版本已变化，请重新打开并检查。"),
    "batch_already_active": ("Your team already has an active evaluation.", "本队已有正在进行的评测。"),
    "preparation_limit": ("Your team already has three projects being prepared.", "本队已有三个项目正在准备，请等待完成。"),
    "preparation_daily_limit": ("Your team has used today’s project preparations (the daily number follows the evaluation quota; see survey26 quota). The count resets at 00:00 UTC (08:00 Beijing time).",
                                "本队今天的项目准备次数已用完（每天的次数与评测次数相同，可用 survey26 quota 查看），每天北京时间 8 点（UTC 0 点）重置。"),
    "daily_limit": ("The daily evaluation limit has been reached.", "今天的评测次数已用完。"),
    "repeat_daily_limit": ("Evaluate 3 times and average needs 3 of today’s evaluations.", "「评测 3 次取平均」需要今天剩余至少 3 次评测。"),
    "revision_already_evaluated": ("This version has already been evaluated. Pass --yes to evaluate it again (uses one more of today’s evaluations).",
                                   "这个版本已经评测过。再评测一次会再占用今天 1 次评测，请加上 --yes。"),
    "revision_withdrawn": ("This version was withdrawn.", "这个版本已撤回。"),
    "revision_not_withdrawable": ("Only versions that are not being prepared and were never evaluated can be withdrawn.", "只能撤回未在准备中、也从未评测过的版本。"),
    "revision_not_found": ("No such version of your team.", "找不到本队的这个版本。"),
    "revision_not_ready": ("This version is not ready yet.", "这个版本还没有准备好。"),
    "revision_not_approved": ("Only a confirmed version can be chosen.", "只能选择已确认的版本。"),
    "wrong_file_type": ("Choose a file with the required extension.", "请选择要求的文件类型。"),
    "file_too_large": ("The file is empty or exceeds the size limit.", "文件为空或超过大小限制。"),
    "invalid_repository_url": ("Enter a public GitHub link: https://github.com/owner/repository, optionally with /tree/<branch>/<folder> or /commit/<sha>.", "请输入公开 GitHub 仓库链接：https://github.com/owner/repository，可带 /tree/分支/子目录 或 /commit/提交号。"),
    "repository_not_found": ("This GitHub repository was not found. Check the owner and name, and that it is public.", "找不到这个 GitHub 仓库，请检查用户名、仓库名，并确认仓库是公开的。"),
    "source_ref_not_found": ("This branch, tag or commit does not exist in the repository.", "仓库里没有这个分支、标签或 commit。"),
    "source_subdir_not_found": ("This folder does not exist at that branch or commit (folder names are case-sensitive).", "在这个分支或 commit 中找不到该子目录（区分大小写）。"),
    "source_options_conflict": ("The branch or folder in the link differs from --branch/--subdir. Keep only one of them.", "链接里的分支或子目录与 --branch/--subdir 不一致，请只保留一处。"),
    "invalid_source_ref": ("The branch, tag or commit name is not valid.", "分支、标签或 commit 名称格式不正确。"),
    "invalid_source_subdir": ("Enter the folder as a relative path such as agent or apps/agent.", "子目录请填写相对路径，例如 agent 或 apps/agent。"),
    "source_options_unavailable": ("Branch and folder choices are temporarily unavailable. Submit the plain repository link, or upload a ZIP.", "暂时不支持指定分支或子目录，请提交仓库主页链接，或上传 ZIP。"),
    "invalid_source_archive": ("This folder contains links or files that cannot be packaged. Upload the project as a ZIP instead.", "该目录包含符号链接或无法打包的文件，请改为上传 ZIP。"),
    "private_source_requires_zip": ("This repository is private. Make it public, or upload the project as a ZIP.", "这个仓库是私有的。请把它设为公开，或改为上传 ZIP。"),
    "source_too_large": ("The repository archive is larger than 100 MB. Upload a smaller ZIP of the project instead.", "仓库压缩包超过 100 MB，请改为上传精简后的项目 ZIP。"),
    "source_snapshot_unavailable": ("Could not save a copy of this repository version right now. Please try again in a minute.", "暂时无法保存该仓库版本的副本，请稍后再试。"),
    "invalid_team_variable": ("Invalid name or value: use upper-case letters, digits and underscores, not a reserved name; the value must not be empty and at most 8 KB.",
                              "变量名或值不符合要求：变量名使用大写字母、数字和下划线，不能使用平台保留的名称；值不能为空且不超过 8 KB。"),
    "no_model_not_available": ("\"Without a model\" (--no-model) is only for project evaluations in the online competition and practice, not the hidden final.",
                               "「本次不提供模型」（--no-model）只能用于正式赛和练习的项目评测，不能用于隐藏卡决赛。"),
    "team_variable_not_found": ("No team variable with this name. See survey26 env show.", "没有这个名称的队伍变量，可用 survey26 env show 查看。"),
    "team_variable_limit": ("The variable limit has been reached; delete a variable you no longer use first.", "变量数量已达上限，请先删除不再使用的变量。"),
    "invalid_egress_route": ("Choose an egress route: direct, cn or overseas.", "请选择出网线路：direct（直连）、cn（回国代理）或 overseas（海外代理）。"),
    "egress_route_unavailable": ("Egress routes are not offered right now; evaluations connect directly.",
                                 "出网线路暂未开放，评测直接连接。"),
    "invalid_team_domains": ("Invalid domain: enter the name only (e.g. api.kimi.com), without https://, a port or a path, and not an IP address or internal name; at most 10.",
                             "域名格式不正确：只填写域名本身（例如 api.kimi.com），不含 https://、端口或路径，不能是 IP 地址或内网名称；最多 10 个。"),
    "team_domain_not_public": ("This domain does not resolve right now, or resolves to a non-public address, so it cannot be added.",
                               "这个域名目前无法解析，或解析到了非公网地址，不能添加。"),
    "final_version_locked": ("The online phase has ended; the final version can no longer change.", "正式赛已结束，最终版本不能再修改。"),
    "upload_limit": ("Too many uploads are still pending for your team. Wait a few minutes for them to clear, then try again.", "本队有太多上传正在等待处理，请等几分钟后再试一次。"),
    "upload_failed": ("The file upload failed, possibly due to the network. Please try again.", "文件上传失败，可能是网络问题，请重试。"),
    "upload_not_found": ("The upload session expired or could not be found. Upload the file again.", "上传会话已过期或找不到，请重新上传。"),
    "upload_not_finished": ("The file has not finished uploading yet. Wait a moment and try again.", "文件还没有上传完成，请稍等再试一次。"),
    "zip_has_no_code": ('No code files were found in the ZIP. Make sure you zipped the folder that contains your program, or start from an official example (the examples include observer.project.json).', '压缩包里没有找到代码文件。请确认打包的是包含程序的文件夹，或参考官方示例，示例自带 observer.project.json。'),
    "portal_unavailable": ("Could not reach the server. Check your connection and try again.", "无法连接服务器，请检查网络后重试。"),
    "phase_closed": ("This phase is not taking evaluations right now.", "这个赛程现在不接受评测。"),
    "no_extra_phase": ("No extra phase is offered right now.", "当前没有开放的额外赛程。"),
    "projects_not_enabled": ("Project evaluation is not open for the current competition.", "当前比赛尚未开放项目评测。"),
    "result_not_ready": ("This card has no result yet.", "这张卡还没有结果。"),
    "project_not_ready": ("This project version cannot be downloaded yet.", "该项目版本暂时无法下载。"),
    "run_not_found": ("No such run of your team.", "找不到本队的这次运行。"),
    # Teams, invitations, profile (website errors and team.errors)
    "already_in_team": ("You are already on a team.", "你已经在一个队伍里了。"),
    "bad_code": ("That invite code is not right.", "邀请码不对。"),
    "full": ("This team is full.", "这个队伍已经满员。"),
    "locked": ("This team is locked and not taking new members.", "这个队伍已锁定，不再接受新成员。"),
    "name_length": ("Team names need 2–60 characters.", "队伍名需要 2–60 个字符。"),
    "name_taken": ("That team name is taken.", "这个队伍名已经有人用了。"),
    "bad_size": ("Team size must be between 1 and 3, and not below the current member count.", "队伍人数上限要在 1 到 3 之间，且不少于现有人数。"),
    "leader_only": ("Only the team leader can do this.", "只有队长能做这项操作。"),
    "leader_must_transfer": ("You are the leader: hand leadership to someone else before leaving.", "你是队长，退出前请先把队长转给别人。"),
    "has_submissions": ("This team already has submissions, so it cannot be disbanded or emptied.", "这个队伍已经有提交记录，不能解散或清空。"),
    "not_in_team": ("You are not on a team.", "你现在不在任何队伍里。"),
    "not_member": ("That person is not on your team.", "这个人不是你们队的成员。"),
    "cannot_remove_leader": ("The leader cannot be removed from the team.", "不能把队长移出队伍。"),
    "need_team": ("Create or join a team first.", "请先创建或加入队伍。"),
    "team_limit_reached": ("All team places are taken and registration is closed, so no new team can be created. You can still join an existing team with an invite code.",
                           "参赛队伍已满，报名已关闭，不能再创建新队伍。你仍可以用邀请码加入已有队伍。"),
    "recipient_unavailable": ("This person cannot currently receive a team invitation.", "对方目前无法接收组队邀请。"),
    "team_unavailable": ("This team is no longer available.", "这个队伍目前不可加入。"),
    "invitation_not_found": ("This request is not addressed to you.", "你不是这条请求的接收方。"),
    "invitation_finished": ("This request has already been handled.", "这条请求已经处理完毕。"),
    "banned": ("This account is suspended.", "这个账号已被停用。"),
    "no_codes_left": ("No codes left from this provider.", "这家的兑换码已经领完了。"),
    "already_assigned": ("This team already has a code from this provider.", "这个队伍已经有这家的兑换码了。"),
    "captain_only": ("Only the team captain can claim the Kimi Coding Plan code.", "只有队长可以领取 Kimi Coding Plan 兑换码。"),
    "not_eligible": ("Your team needs one successful score in the practice round first.", "你的队伍还需在练习赛获得一次成功评分。"),
    "nickname_too_long": ("Nickname must be 40 characters or fewer.", "昵称最多 40 个字。"),
    "name_required": ("Name is required.", "请填写姓名。"),
    "avatar_bad_type": ("Please choose a PNG, JPG or WEBP image.", "请选择 PNG、JPG 或 WEBP 图片。"),
    "avatar_too_large": ("That image is larger than 2 MB.", "图片超过 2 MB。"),
    "avatar_invalid_image": ("This file is not a readable image.", "无法读取这张图片。"),
    "invalid_field": ("A field is missing or invalid.", "有字段缺失或不符合要求。"),
    # Friends and invitations by UID
    "invalid_uid": ("A UID is the 9-digit number shown at the bottom right of the website (e.g. 100000123).",
                    "UID 是网站右下角显示的 9 位数字（例如 100000123）。"),
    "uid_self": ("That is your own UID.", "这是你自己的 UID。"),
    "uid_unavailable": ("No one can be reached with this UID. Check the number.", "无法通过这个 UID 找到可添加的人，请检查号码。"),
    "uid_not_found": ("No participant has this UID. Check the number.", "没有参赛者使用这个 UID，请检查号码。"),
    "uid_daily_limit": ("You have used today's 20 actions by UID. Try again tomorrow.", "今天通过 UID 添加或邀请的 20 次机会已用完，请明天再试。"),
    "blocked_by_you": ("You have blocked this person. Unblock them first.", "你已屏蔽此人，请先解除屏蔽。"),
    "recipient_in_team": ("This person is already on a team.", "对方已经在一个队伍里了。"),
    "request_not_found": ("This friend request is not addressed to you.", "你不是这条好友请求的接收方。"),
    "request_finished": ("This friend request has already been handled.", "这条好友请求已经处理完毕。"),
    "not_friends": ("You are not friends with this person.", "你们还不是好友。"),
    "user_not_found": ("No such person.", "找不到这个人。"),
}

# Error codes of the by-UID actions (returned as {"error": code}) as the CLI's codes.
UID_ERRORS = {"self": "uid_self", "daily_limit": "uid_daily_limit"}


def exit_code_for(code: str) -> int:
    if code in AUTH_CODES:
        return EXIT_AUTH
    if code == "rate_limited":
        return EXIT_RATE
    if code in LIMIT_CODES:
        return EXIT_LIMIT
    if code in NOT_FOUND_CODES:
        return EXIT_NOT_FOUND
    if code in UNAVAILABLE_CODES:
        return EXIT_UNAVAILABLE
    if code in ("confirmation_required", "usage", "ambiguous_id", "file_not_found", "variables_exist"):
        return EXIT_USAGE
    if code == "wait_timeout":
        return EXIT_TIMEOUT
    return EXIT_ERROR


class CliError(Exception):
    def __init__(self, code: str, message: str = "", exit_code: int | None = None, status: int | None = None):
        super().__init__(code)
        self.code = code
        self.detail = message
        self.exit_code = exit_code if exit_code is not None else exit_code_for(code)
        self.status = status


def _sleep(seconds: float) -> None:
    """Every pause of the tool; SURVEY26_SLEEP_SCALE (tests) scales them."""
    time.sleep(seconds * float(os.environ.get("SURVEY26_SLEEP_SCALE") or 1))


def language() -> str:
    lang = os.environ.get("SURVEY26_LANG") or os.environ.get("LC_ALL") or os.environ.get("LANG") or ""
    return "zh" if lang.lower().startswith("zh") else "en"


def message_for(code: str, lang: str, detail: str = "") -> str:
    pair = MESSAGES.get(code)
    if pair:
        return pair[1] if lang == "zh" else pair[0]
    return detail or code


# ---------------------------------------------------------------------------------------------
# Configuration and transport


def config_path() -> Path:
    if os.environ.get("SURVEY26_CONFIG"):
        return Path(os.environ["SURVEY26_CONFIG"])
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "survey26" / "config.json"


def read_config() -> dict:
    try:
        return json.loads(config_path().read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def write_config(data: dict) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(str(tmp), str(path))
    return path


class Api:
    """Talks to the survey26-cli gateway. Reads are retried on network errors; writes only when
    the connection could not be opened at all (the request never reached the server)."""

    def __init__(self, token: str | None, url: str, timeout: float = 60.0, retries: int = 3):
        self.token, self.url, self.timeout, self.retries = token, url, timeout, retries

    def call(self, op: str, write: bool = False, **fields):
        if not self.token:
            raise CliError("no_token")
        if not TOKEN_RE.match(self.token):
            raise CliError("invalid_token")
        body = json.dumps({"op": op, **fields}).encode("utf-8")
        attempt = 0
        while True:
            attempt += 1
            request = urllib.request.Request(self.url, data=body, method="POST", headers={
                "Authorization": "Bearer " + self.token, "Content-Type": "application/json",
                "User-Agent": "survey26-cli/" + __version__})
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8") or "{}")
                    if not isinstance(payload, dict) or "data" not in payload:
                        raise CliError("invalid_api_response")
                    return payload["data"]
            except urllib.error.HTTPError as error:
                try:
                    code = str(json.loads(error.read().decode("utf-8")).get("error") or "request_failed")
                except (ValueError, AttributeError):
                    code = "request_failed"
                if "violates foreign key constraint" in code and "on table \"teams\"" in code:
                    code = "has_submissions"  # a team with projects cannot be emptied or disbanded
                retry = error.code in (502, 503, 504) and not write
                if not retry or attempt >= self.retries:
                    raise CliError(code, status=error.code)
            except urllib.error.URLError as error:
                # Refused or unresolvable: the request never left this computer, so a retry is safe.
                never_sent = isinstance(error.reason, (ConnectionRefusedError, socket.gaierror))
                if attempt >= self.retries or (write and not never_sent):
                    raise CliError("network_error", str(error.reason))
            except (OSError, TimeoutError, http.client.HTTPException) as error:
                # Timed out, reset or cut short after sending: a write may have been applied, so it is not repeated.
                if attempt >= self.retries or write:
                    raise CliError("network_error", str(error))
            _sleep(min(2 ** attempt, 8))

    def rpc(self, name: str, write: bool = False, **args):
        return self.call("rpc", write=write, name=name, args=args)

    def portal(self, action: str, write: bool = False, **fields):
        return self.call("portal", write=write, fields={"action": action, **fields})


def http_get(url: str, timeout: float = 120.0, retries: int = 3) -> bytes:
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "survey26-cli/" + __version__}),
                                        timeout=timeout) as response:
                return response.read()
        except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException) as error:
            if attempt == retries:
                raise CliError("download_failed", str(getattr(error, "reason", error)))
            _sleep(attempt * 2)
    raise CliError("download_failed")


def http_put(url: str, data: bytes, headers: dict, timeout: float = 600.0) -> None:
    request = urllib.request.Request(url, data=data, method="PUT", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
    except urllib.error.HTTPError as error:
        text = error.read().decode("utf-8", "replace")
        # Re-sending a part that already arrived is fine (the website treats it the same way).
        if error.code in (400, 409) and ("Duplicate" in text or "already exists" in text):
            return
        raise CliError("upload_failed", text[:200])
    except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException) as error:
        raise CliError("upload_failed", str(getattr(error, "reason", error)))


# ---------------------------------------------------------------------------------------------
# Output


class Out:
    def __init__(self, as_json: bool, lang: str, command: str):
        self.json, self.lang, self.command = as_json, lang, command

    def t(self, en: str, zh: str) -> str:
        return zh if self.lang == "zh" else en

    def line(self, text: str = "") -> None:
        if not self.json:
            print(text, flush=True)

    def table(self, rows: list, columns: list) -> None:
        if self.json:
            return
        if not rows:
            print(self.t("(none)", "（无）"))
            return
        cells = [[str(c[0]) for c in columns]] + [["" if r.get(c[1]) is None else str(r.get(c[1])) for c in columns] for r in rows]
        widths = [max(len(row[i]) for row in cells) for i in range(len(columns))]
        for index, row in enumerate(cells):
            print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
            if index == 0:
                print("  ".join("-" * w for w in widths))


def interactive(args) -> bool:
    return not args.json and sys.stdin.isatty() and sys.stdout.isatty()


def confirm(args, out: Out, question_en: str, question_zh: str) -> None:
    """The website asks before quota-consuming or irreversible actions; so does the CLI. Without a
    terminal (or with --json) it never prompts: pass --yes."""
    if getattr(args, "yes", False):
        return
    if interactive(args):
        answer = input(out.t(question_en, question_zh) + " [y/N] ").strip().lower()
        if answer in ("y", "yes", "是"):
            return
        raise CliError("cancelled", out.t("Cancelled.", "已取消。"), EXIT_USAGE)
    raise CliError("confirmation_required", out.t(question_en, question_zh))


# ---------------------------------------------------------------------------------------------
# Helpers shared by commands


# A1-D1 cards (v4-a1 ... v4-d1, optionally with a -vN version suffix): listed after A-H.
SECOND_CARD_RE = re.compile(r"^v4-([a-d])1(-v[0-9]+)?\Z")


def scenario_label(slug: str, name: str, lang: str) -> str:
    m = re.match(r"^v4-practice-(alpha|beta|gamma|delta)$", slug or "")
    if m:
        symbol = {"alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ"}[m.group(1)]
        return ("练习卡 " if lang == "zh" else "Practice card ") + symbol
    m = re.match(r"^v4-([a-h])$", slug or "")
    if m:
        return ("任务卡 " if lang == "zh" else "Card ") + m.group(1).upper()
    m = SECOND_CARD_RE.match(slug or "")
    if m:
        return ("任务卡 " if lang == "zh" else "Card ") + m.group(1).upper() + "1"
    return name or slug or ""


def scenario_order(slug: str) -> float:
    m = re.match(r"^v4-practice-(alpha|beta|gamma|delta)$", slug or "")
    if m:
        return ["alpha", "beta", "gamma", "delta"].index(m.group(1))
    m = re.match(r"^v4-([a-h])$", slug or "")
    if m:
        return ord(m.group(1)) - ord("a")
    m = SECOND_CARD_RE.match(slug or "")
    if m:
        return 8 + ord(m.group(1)) - ord("a")
    return float("inf")


def card_folder_name(slug: str, fallback: str) -> str:
    folder = re.sub(r"[^a-zA-Z0-9._-]+", "-", re.sub(r"^v4-", "", slug or "")).strip("-")
    return folder or fallback


def ordered_card_folder(index: int, total: int, folder: str) -> str:
    return str(index + 1).zfill(len(str(total))) + "-" + folder


def flatten_result_entries(entries: dict) -> dict:
    """Same as the website: drop the single '<repository>-<commit>/' wrapper of a result kept on GitHub."""
    names = [n for n in entries if not n.endswith("/")]
    nested = [n for n in names if "/" in n]
    tops = {n[:n.index("/") + 1] for n in nested}
    wrapped = len(tops) == 1 and all("/" in n or n == "agent.log" for n in names)
    top = next(iter(tops)) if wrapped else ""
    out: dict = {}
    for name in names:
        flat = name[len(top):] if top and name.startswith(top) else name
        if flat not in out or name == flat:
            out[flat] = entries[name]
    return out


def resolve_id(prefix: str, ids: list, kind: str) -> str:
    """Full UUIDs pass through; a prefix (at least 4 characters) must match exactly one known ID."""
    value = (prefix or "").strip().lower()
    if UUID_RE.match(value):
        return value
    if len(value) < 4:
        raise CliError("usage", "%s ID: give the full ID or at least 4 characters" % kind, EXIT_USAGE)
    matches = sorted({i for i in ids if i.startswith(value)})
    if len(matches) > 1:
        raise CliError("ambiguous_id")
    if not matches:
        raise CliError("not_found", "no %s matches %s" % (kind, prefix), EXIT_NOT_FOUND)
    return matches[0]


def portal_list(api: Api) -> dict:
    return api.portal("list") or {}


def polled_list(api: Api, deadline: float) -> dict:
    """portal_list for wait loops: a rate limit or a temporary outage pauses the wait instead of ending it."""
    while True:
        try:
            return portal_list(api)
        except CliError as error:
            if error.exit_code not in (EXIT_RATE, EXIT_UNAVAILABLE) or time.time() >= deadline:
                raise
            _sleep(60 if error.exit_code == EXIT_RATE else 20)


def all_revisions(data: dict) -> list:
    rows = []
    for project in data.get("projects") or []:
        for r in project.get("observer_revisions") or []:
            rows.append(dict(r, title=project.get("title")))
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows


def find_revision(data: dict, prefix: str) -> dict:
    revisions = all_revisions(data)
    rid = resolve_id(prefix, [r["id"] for r in revisions], "version")
    for r in revisions:
        if r["id"] == rid:
            return r
    raise CliError("revision_not_found")


def find_batch(data: dict, prefix: str) -> dict:
    batches = data.get("batches") or []
    if prefix in ("latest", "last"):
        if not batches:
            raise CliError("not_found", "no evaluations yet", EXIT_NOT_FOUND)
        return batches[0]
    bid = resolve_id(prefix, [b["id"] for b in batches], "evaluation")
    for b in batches:
        if b["id"] == bid:
            return b
    raise CliError("not_found", "no such evaluation of your team (only the latest 50 are listed)", EXIT_NOT_FOUND)


def find_run(data: dict, prefix: str) -> tuple:
    runs = [(b, r) for b in data.get("batches") or [] for r in b.get("observer_runs") or []]
    if UUID_RE.match((prefix or "").lower()):
        for b, r in runs:
            if r["id"] == prefix.lower():
                return b, r
        return None, {"id": prefix.lower(), "scenario_id": None}
    rid = resolve_id(prefix, [r["id"] for _, r in runs], "run")
    for b, r in runs:
        if r["id"] == rid:
            return b, r
    raise CliError("run_not_found")


def scenario_names(api: Api, data: dict) -> dict:
    ids = sorted({r.get("scenario_id") for b in data.get("batches") or [] for r in b.get("observer_runs") or []
                  if r.get("scenario_id")})
    names = {}
    for start in range(0, len(ids), 100):
        for row in api.call("scenarios", ids=ids[start:start + 100]) or []:
            names[row["id"]] = row
    return names


def competition_phase(api: Api, data: dict, wanted_slug: str | None) -> dict:
    """The phase the website's evaluate button uses (the entry phase first), or --phase."""
    now = time.time()

    def ts(value):
        if not value:
            return None
        return _parse_time(value)

    phases = []
    for p in data.get("phases") or []:
        info = p.get("phases") or {}
        ends, starts = ts(info.get("ends_at")), ts(info.get("starts_at"))
        if info.get("is_active") and (ends is None or ends > now) and (starts is None or starts <= now):
            phases.append(dict(p, slug=info.get("slug"), name_en=info.get("name_en"), name_zh=info.get("name_zh"),
                               ends_at=info.get("ends_at")))
    comp = api.rpc("current_competition") or {}
    extra = comp.get("extra_phase_id") or None
    if wanted_slug == "extra":
        if not extra:
            raise CliError("no_extra_phase")
        wanted_slug = extra
    if wanted_slug:
        for p in phases:
            if p["slug"] == wanted_slug or p["phase_id"] == wanted_slug:
                return dict(p, extra=p["phase_id"] == extra)
        raise CliError("phase_closed")
    beta = None
    if comp.get("mode") == "competition":
        try:
            beta = api.rpc("my_observer_phase")
        except CliError:
            beta = None
    # The optional extra (unscored) phase is only used when asked for (--phase extra or its slug).
    allowed = [x for x in (beta, comp.get("project_phase_id"), comp.get("phase_id")) if x and x != extra]
    candidates = [p for p in phases if p["phase_id"] in allowed]
    for preferred in allowed:
        for p in candidates:
            if p["phase_id"] == preferred:
                return dict(p, extra=False)
    raise CliError("no_open_phase")


def extra_phase_id(api: Api):
    """The optional extra (unscored) phase of current_competition(), or None (also when the lookup fails)."""
    try:
        comp = api.rpc("current_competition")
    except CliError:
        return None
    return (comp.get("extra_phase_id") or None) if isinstance(comp, dict) else None


def phase_filter(api: Api, data: dict, wanted: str | None):
    """--phase of the listing commands: a phase slug or ID of your team's phases, or 'extra'."""
    if not wanted:
        return None
    if wanted == "extra":
        extra = (api.rpc("current_competition") or {}).get("extra_phase_id")
        if not extra:
            raise CliError("no_extra_phase")
        return extra
    for p in data.get("phases") or []:
        if p.get("phase_id") == wanted or (p.get("phases") or {}).get("slug") == wanted:
            return p["phase_id"]
    raise CliError("not_found", "no such phase: %s" % wanted, EXIT_NOT_FOUND)


def in_phase(data: dict, phase_id) -> dict:
    if not phase_id:
        return data
    return dict(data, batches=[b for b in data.get("batches") or [] if b.get("phase_id") == phase_id])


def phase_slugs(data: dict) -> dict:
    return {p.get("phase_id"): (p.get("phases") or {}).get("slug") for p in data.get("phases") or []}


def _parse_time(value: str) -> float | None:
    from datetime import datetime
    try:
        text = value.replace("Z", "+00:00")
        if re.search(r"\.\d+", text):
            text = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], text)
        if re.search(r"[+-]\d\d$", text):
            text += ":00"
        return datetime.fromisoformat(text).timestamp()
    except (ValueError, AttributeError):
        return None


def batch_summary(batch: dict, names: dict, lang: str, slugs: dict) -> dict:
    runs = []
    for run in batch.get("observer_runs") or []:
        sc = names.get(run.get("scenario_id")) or {}
        runs.append({
            "run_id": run["id"], "scenario_id": run.get("scenario_id"), "card": sc.get("slug"),
            "label": scenario_label(sc.get("slug") or "", sc.get("name") or "", lang),
            "status": run.get("status"), "score": run.get("score"), "has_result": bool(run.get("result_path")),
        })
    runs.sort(key=lambda r: scenario_order(r["card"] or ""))
    return {
        "batch_id": batch["id"], "status": batch.get("status"), "score": batch.get("score"),
        "phase_id": batch.get("phase_id"), "phase": slugs.get(batch.get("phase_id")), "revision_id": batch.get("revision_id"),
        "created_at": batch.get("created_at"), "quota_refunded": bool(batch.get("quota_refunded")),
        "repeat_group": batch.get("repeat_group"), "repeat_runs": batch.get("repeat_runs"),
        "model_disabled": bool(batch.get("model_disabled")), "runs": runs,
    }


def fmt_score(value) -> str:
    return "-" if value is None else ("%.2f" % float(value))


# ---------------------------------------------------------------------------------------------
# Commands: account and profile


def cmd_login(api: Api, args, out: Out):
    token = getattr(args, "token", None)
    if args.token_stdin:
        token = sys.stdin.readline().strip()
    if not token:
        if interactive(args):
            token = getpass.getpass(out.t("API token (s26_...): ", "API 令牌（s26_...）：")).strip()
        else:
            raise CliError("usage", "pass --token TOKEN or --token-stdin", EXIT_USAGE)
    api.token = token
    me = api.call("whoami")
    path = write_config(dict(read_config(), token=token))
    who = (me or {}).get("me") or {}
    out.line(out.t("Logged in as %s (%s). Token saved to %s", "已登录：%s（%s）。令牌已保存到 %s")
             % (who.get("nickname") or who.get("name"), who.get("email"), path))
    return {"user": _public_me(who), "config_path": str(path)}


def cmd_logout(api: Api, args, out: Out):
    config = read_config()
    removed = config.pop("token", None) is not None
    if removed:
        write_config(config)
    out.line(out.t("Token removed from this computer. It stays valid until you revoke it on your profile page.",
                   "已从本机删除令牌。令牌本身仍有效，如需作废请在个人资料页撤销。"))
    return {"removed": removed, "config_path": str(config_path())}


def _public_me(me: dict) -> dict:
    keys = ("id", "uid", "email", "name", "nickname", "github", "affiliation", "role", "locale", "city", "contact", "blurb",
            "show_on_wall", "looking_for_team", "seeking", "seeking_count", "astro_level", "ai_level", "avatar_url")
    result = {k: me.get(k) for k in keys}
    team = me.get("team")
    result["team"] = None if not team else {k: team.get(k) for k in (
        "id", "name", "leader_id", "invite_code", "max_size", "member_count", "is_locked", "github_repo", "project_idea")}
    if team:
        result["team"]["is_captain"] = team.get("leader_id") == me.get("id")
    return result


def cmd_whoami(api: Api, args, out: Out):
    me = _public_me((api.call("whoami") or {}).get("me") or {})
    out.line("%s  %s" % (me["nickname"] or me["name"], me["email"]))
    if me.get("uid"):
        out.line("UID " + str(me["uid"]))
    team = me["team"]
    out.line(out.t("Team: ", "队伍：") + (("%s (%s)" % (team["name"], out.t("captain", "队长") if team["is_captain"] else out.t("member", "队员")))
                                          if team else out.t("none", "无")))
    return me


def cmd_profile_show(api: Api, args, out: Out):
    me = _public_me((api.call("whoami") or {}).get("me") or {})
    for key, value in me.items():
        if key != "team":
            out.line("%-16s %s" % (key, "" if value is None else value))
    return me


PROFILE_OPTIONS = ("name", "nickname", "github", "affiliation", "role", "locale", "city", "contact", "blurb")


def cmd_profile_set(api: Api, args, out: Out):
    fields = {k: getattr(args, k) for k in PROFILE_OPTIONS if getattr(args, k) is not None}
    if args.astro_level is not None:
        fields["astro_level"] = args.astro_level
    if args.ai_level is not None:
        fields["ai_level"] = args.ai_level
    if not fields:
        raise CliError("usage", "nothing to change (see survey26 profile set --help)", EXIT_USAGE)
    if "name" in fields and not fields["name"].strip():
        raise CliError("name_required")
    if "nickname" in fields and len(fields["nickname"].strip()) > 40:
        raise CliError("nickname_too_long")
    if "github" in fields:
        fields["github"] = fields["github"].strip().lstrip("@")
    if "blurb" in fields:
        fields["blurb"] = fields["blurb"].strip()[:160]
    fields = {k: (v.strip() if isinstance(v, str) else v) for k, v in fields.items()}
    me = _public_me((api.call("profile_update", write=True, fields=fields) or {}).get("me") or {})
    out.line(out.t("Profile saved.", "资料已保存。"))
    return me


def cmd_avatar_set(api: Api, args, out: Out):
    path = Path(args.file)
    if not path.is_file():
        raise CliError("file_not_found")
    data = path.read_bytes()
    kind = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg" if data[:3] == b"\xff\xd8\xff" \
        else "image/webp" if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else ""
    if not kind:
        raise CliError("avatar_bad_type")
    if len(data) > 2 * 1024 * 1024:
        raise CliError("avatar_too_large")
    result = api.call("avatar_upload", write=True, content_type=kind, data=base64.b64encode(data).decode("ascii"))
    out.line(out.t("Avatar updated: ", "头像已更新：") + str((result or {}).get("avatar_url")))
    return result


def cmd_avatar_clear(api: Api, args, out: Out):
    result = api.call("avatar_clear", write=True)
    out.line(out.t("Avatar removed.", "头像已移除。"))
    return result


# ---------------------------------------------------------------------------------------------
# Commands: find teammates


def cmd_teammates_list(api: Api, args, out: Out):
    rows = api.rpc("participants_wall", p_limit=args.limit) or []
    if args.looking:
        rows = [r for r in rows if r.get("looking_for_team") or r.get("seeking")]
    out.table(rows, [("ID", "id"), (out.t("Name", "名字"), "name"), (out.t("Seeking", "寻找"), "seeking"),
                     (out.t("Team", "队伍"), "team_name"), (out.t("About", "简介"), "blurb")])
    return rows


def cmd_teammates_contact(api: Api, args, out: Out):
    result = api.rpc("teammate_contact", p_id=args.user_id)
    out.line(json.dumps(result, ensure_ascii=False))
    return result


def cmd_teammates_invite(api: Api, args, out: Out):
    result = api.rpc("send_team_invite", write=True, p_recipient=args.user_id)
    out.line(out.t("Invitation sent.", "邀请已发送。"))
    return result


def cmd_teammates_visibility(api: Api, args, out: Out):
    me = _public_me((api.call("whoami") or {}).get("me") or {})
    if args.state == "show":
        view = {k: me[k] for k in ("show_on_wall", "blurb", "contact", "seeking", "seeking_count", "looking_for_team")}
        for k, v in view.items():
            out.line("%-18s %s" % (k, "" if v is None else v))
        return view
    seeking = args.seeking if args.seeking is not None else (me.get("seeking") or "")
    count = args.seeking_count if args.seeking_count is not None else (me.get("seeking_count") or 1)
    fields = {
        "show_on_wall": args.state == "on",
        "blurb": (args.blurb if args.blurb is not None else me.get("blurb") or "").strip()[:160],
        "contact": (args.contact if args.contact is not None else me.get("contact") or "").strip()[:200],
        "seeking": seeking, "seeking_count": count if seeking else 0, "looking_for_team": seeking != "",
    }
    api.call("profile_update", write=True, fields=fields)
    out.line(out.t("You are now listed on Find teammates." if fields["show_on_wall"] else "You are no longer listed on Find teammates.",
                   "你已出现在「找队友」页面。" if fields["show_on_wall"] else "你已不在「找队友」页面显示。"))
    return fields


# ---------------------------------------------------------------------------------------------
# Commands: team


def _me(api: Api) -> dict:
    return (api.call("whoami") or {}).get("me") or {}


def _team_or_fail(me: dict) -> dict:
    if not me.get("team"):
        raise CliError("need_team")
    return me["team"]


def cmd_team_show(api: Api, args, out: Out):
    me = _me(api)
    team = me.get("team")
    if not team:
        out.line(out.t("You are not on a team. Create one (survey26 team create NAME) or join one (survey26 team join CODE).",
                       "你还没有队伍。可创建（survey26 team create 队名）或加入（survey26 team join 邀请码）。"))
        return {"team": None, "members": []}
    members = api.rpc("team_members", p_team_id=team["id"]) or []
    info = _public_me(me)["team"]
    out.line("%s  (%s/%s%s)" % (team["name"], len(members), team["max_size"], out.t(", locked", "，已锁定") if team.get("is_locked") else ""))
    out.line(out.t("Invite code: ", "邀请码：") + str(team.get("invite_code")) + "   " + SITE + "/team?invite=" + str(team.get("invite_code")))
    out.table([dict(m, leader="*" if m.get("is_leader") else "") for m in members],
              [("ID", "id"), (out.t("Name", "名字"), "name"), (out.t("Captain", "队长"), "leader"), ("GitHub", "github")])
    return {"team": info, "members": members}


def cmd_team_members(api: Api, args, out: Out):
    team = _team_or_fail(_me(api))
    members = api.rpc("team_members", p_team_id=team["id"]) or []
    out.table([dict(m, leader="*" if m.get("is_leader") else "") for m in members],
              [("ID", "id"), (out.t("Name", "名字"), "name"), (out.t("Captain", "队长"), "leader"), ("GitHub", "github"),
               (out.t("Affiliation", "单位"), "affiliation")])
    return members


def cmd_team_create(api: Api, args, out: Out):
    api.rpc("create_team", write=True, p_name=args.name.strip(), p_max_size=args.max_size,
            p_project_idea=(args.idea or "").strip(), p_github_repo=(args.repo or "").strip())
    team = _public_me(_me(api))["team"]
    out.line(out.t("Team created: %s. Invite code: %s", "队伍已创建：%s。邀请码：%s") % (team["name"], team["invite_code"]))
    return team


def cmd_team_join(api: Api, args, out: Out):
    api.rpc("join_team", write=True, p_invite_code=args.code.strip().upper())
    team = _public_me(_me(api))["team"]
    out.line(out.t("You joined %s.", "你已加入 %s。") % (team or {}).get("name"))
    return team


def cmd_team_leave(api: Api, args, out: Out):
    confirm(args, out, "Leave your team?", "确定退出队伍吗？")
    api.rpc("leave_team", write=True)
    out.line(out.t("You left the team.", "你已退出队伍。"))
    return {"left": True}


def cmd_team_set(api: Api, args, out: Out):
    team = _team_or_fail(_me(api))
    lock = team.get("is_locked") if args.lock is None else args.lock
    name = args.name.strip() if args.name is not None and args.name.strip() != team["name"] else None
    api.rpc("update_team", write=True,
            p_project_idea=(args.idea if args.idea is not None else team.get("project_idea") or "").strip(),
            p_github_repo=(args.repo if args.repo is not None else team.get("github_repo") or "").strip(),
            p_max_size=args.max_size if args.max_size is not None else team["max_size"], p_is_locked=bool(lock), p_name=name)
    team = _public_me(_me(api))["team"]
    out.line(out.t("Team saved.", "队伍信息已保存。"))
    return team


def cmd_team_code(api: Api, args, out: Out):
    if args.regenerate:
        api.rpc("regenerate_invite_code", write=True)
    team = _team_or_fail(_me(api))
    link = SITE + "/team?invite=" + str(team["invite_code"])
    out.line(out.t("Invite code: ", "邀请码：") + str(team["invite_code"]))
    out.line(out.t("Invite link: ", "邀请链接：") + link)
    return {"invite_code": team["invite_code"], "invite_link": link, "regenerated": bool(args.regenerate)}


def cmd_team_transfer(api: Api, args, out: Out):
    confirm(args, out, "Make this member the captain? You will no longer be the captain.", "确定把队长转给这位成员吗？转让后你将不再是队长。")
    api.rpc("transfer_leadership", write=True, p_user_id=args.user_id)
    out.line(out.t("Captain changed.", "已转让队长。"))
    return {"leader_id": args.user_id}


def cmd_team_kick(api: Api, args, out: Out):
    confirm(args, out, "Remove this member from the team?", "确定把这位成员移出队伍吗？")
    api.rpc("remove_member", write=True, p_user_id=args.user_id)
    out.line(out.t("Member removed.", "已移出该成员。"))
    return {"removed": args.user_id}


def cmd_team_disband(api: Api, args, out: Out):
    confirm(args, out, "Disband the team? This cannot be undone.", "确定解散队伍吗？此操作无法撤销。")
    api.rpc("disband_team", write=True)
    out.line(out.t("Team disbanded.", "队伍已解散。"))
    return {"disbanded": True}


def cmd_team_directory(api: Api, args, out: Out):
    rows = api.rpc("team_directory") or []
    out.table(rows, [("ID", "id"), (out.t("Team", "队伍"), "name"), (out.t("Members", "人数"), "member_count"),
                     (out.t("Max", "上限"), "max_size"), (out.t("Idea", "想法"), "project_idea")])
    return rows


def cmd_team_request(api: Api, args, out: Out):
    result = api.rpc("request_team_join", write=True, p_team_id=args.team_id)
    out.line(out.t("Request sent to the captain.", "已向队长发送申请。"))
    return result


def cmd_team_capacity(api: Api, args, out: Out):
    result = api.rpc("team_capacity")
    out.line(json.dumps(result, ensure_ascii=False))
    return result


# ---------------------------------------------------------------------------------------------
# Commands: invitations (notifications page)


def cmd_invites_list(api: Api, args, out: Out):
    rows = api.rpc("my_team_invitations") or []
    received = [r for r in rows if r.get("direction") == "received"]
    if received and not args.keep_unread:
        latest = max((r.get("updated_at") or "") for r in received)
        try:
            api.rpc("mark_team_invitations_read", write=True, p_ids=[r["id"] for r in received], p_through=latest)
        except CliError:
            pass
    out.table(rows, [("ID", "id"), (out.t("Direction", "方向"), "direction"), (out.t("Kind", "类型"), "kind"),
                     (out.t("Status", "状态"), "status"), (out.t("Team", "队伍"), "team_name"),
                     (out.t("Updated", "更新时间"), "updated_at")])
    return rows


def cmd_invites_respond(api: Api, args, out: Out):
    accept = args.invites_cmd == "accept"
    result = api.rpc("respond_team_invite", write=True, p_invitation=args.invitation_id, p_accept=accept)
    out.line(out.t("Accepted." if accept else "Declined.", "已接受。" if accept else "已拒绝。"))
    return result


def cmd_invites_cancel(api: Api, args, out: Out):
    result = api.rpc("cancel_team_invite", write=True, p_invitation=args.invitation_id)
    out.line(out.t("Cancelled.", "已撤回。"))
    return result


# ---------------------------------------------------------------------------------------------
# Commands: friends and invitations by UID


def _uid(text: str) -> int:
    text = (text or "").strip()
    if not re.fullmatch(r"[1-9][0-9]{8}", text):
        raise CliError("invalid_uid", exit_code=EXIT_USAGE)
    return int(text)


def _uid_result(result) -> dict:
    result = result if isinstance(result, dict) else {}
    if result.get("error"):
        code = str(result["error"])
        raise CliError(UID_ERRORS.get(code, code))
    return result


def cmd_friends_list(api: Api, args, out: Out):
    data = api.rpc("my_friends") or {}
    out.line(out.t("Your UID: ", "你的 UID：") + str(data.get("uid") or ""))
    for title, key, columns in (
            (out.t("Friends", "好友"), "friends", [("UID", "uid"), (out.t("Name", "名字"), "name"), (out.t("Team", "队伍"), "team_name"),
                                                 ("USER_ID", "user_id")]),
            (out.t("Requests to you", "收到的请求"), "incoming", [("ID", "id"), (out.t("Name", "名字"), "name"), ("USER_ID", "user_id"),
                                                             (out.t("Sent", "时间"), "created_at")]),
            (out.t("Your pending requests", "你发出的请求"), "outgoing", [("ID", "id"), ("UID", "uid"), (out.t("Sent", "时间"), "created_at")]),
            (out.t("Blocked", "已屏蔽"), "blocked", [("USER_ID", "user_id"), (out.t("Name", "名字"), "name")])):
        out.line()
        out.line(title)
        out.table(data.get(key) or [], columns)
    return data


def cmd_friends_add(api: Api, args, out: Out):
    result = _uid_result(api.rpc("send_friend_request", write=True, p_uid=_uid(args.uid)))
    status = result.get("status")
    out.line(out.t("You are now friends." if status == "accepted" else "You are already friends." if status == "already_friends"
                   else "Friend request sent.",
                   "你们已成为好友。" if status == "accepted" else "你们已经是好友了。" if status == "already_friends" else "好友请求已发送。"))
    return result


def cmd_friends_respond(api: Api, args, out: Out):
    accept = args.cmd == "accept"
    result = api.rpc("respond_friend_request", write=True, p_request=args.request_id, p_accept=accept)
    out.line(out.t("Accepted." if accept else "Declined.", "已接受。" if accept else "已拒绝。"))
    return result


def cmd_friends_cancel(api: Api, args, out: Out):
    result = api.rpc("cancel_friend_request", write=True, p_request=args.request_id)
    out.line(out.t("Cancelled.", "已撤回。"))
    return result


def cmd_friends_remove(api: Api, args, out: Out):
    result = api.rpc("remove_friend", write=True, p_user=args.user_id)
    out.line(out.t("Removed from your friends.", "已删除好友。"))
    return result


def cmd_friends_block(api: Api, args, out: Out):
    block = args.cmd == "block"
    result = api.rpc("block_user" if block else "unblock_user", write=True, p_user=args.user_id)
    out.line(out.t("Blocked." if block else "Unblocked.", "已屏蔽。" if block else "已解除屏蔽。"))
    return result


def cmd_team_invite_uid(api: Api, args, out: Out):
    result = _uid_result(api.rpc("send_team_invite_by_uid", write=True, p_uid=_uid(args.uid)))
    out.line(out.t("Invitation sent.", "邀请已发送。"))
    return result


# ---------------------------------------------------------------------------------------------
# Commands: team variables and allowed domains


def _print_env(out: Out, env: dict) -> None:
    out.line(out.t("Variables:", "变量："))
    out.table([{"name": v["name"], "kind": out.t("secret", "密文") if v.get("secret") else out.t("plain", "明文"),
                "value": v.get("masked_value") if v.get("secret") else v.get("value"),
                "flags": " ".join(([out.t("model", "模型")] if v.get("model") else []) + ([out.t("off", "已停用")] if v.get("disabled") else [])),
                "updated_at": v.get("updated_at")} for v in env.get("variables") or []],
              [(out.t("Name", "名称"), "name"), (out.t("Kind", "类型"), "kind"), (out.t("Value", "值"), "value"),
               (out.t("Flags", "标记"), "flags"), (out.t("Updated", "更新时间"), "updated_at")])
    if env.get("open"):
        out.line(out.t("Network: any public address over HTTPS (443) and HTTP (80); private and metadata addresses "
                       "are unreachable; every destination is logged (no content). No domain list is needed.",
                       "网络：可访问公网上的任何地址（HTTPS 443、HTTP 80 端口），内网和元数据地址不可访问；"
                       "每个访问地址都会被记录（不含内容），无需登记域名。"))
    else:
        out.line(out.t("Allowed domains: ", "允许访问的域名：") + (", ".join(env.get("domains") or []) or out.t("(none)", "（无）")))
    if env.get("egress_route"):
        out.line(_route_text(out, env["egress_route"]))


ROUTE_LABELS = {"direct": ("direct", "直连"), "cn": ("China route", "回国代理"), "overseas": ("overseas route", "海外代理")}


def _route_text(out: Out, route: dict) -> str:
    name = route.get("route") or "direct"
    label = out.t(*ROUTE_LABELS.get(name, (name, name)))
    text = out.t("Egress route: ", "出网线路：") + label
    if name != "direct":
        text += out.t(" (automatic fallback to direct: %s)", "（节点不可用时自动改为直连：%s）") % (
            out.t("on", "开") if route.get("auto_fallback", True) else out.t("off", "关"))
    if not route.get("available"):
        text += out.t(" - not offered right now; evaluations connect directly", "（暂未开放，评测直接连接）")
    return text


def _env_view(env: dict) -> dict:
    env = dict(env or {})
    env["variables"] = [{"name": v.get("name"), "secret": bool(v.get("secret")),
                         "masked_value": ("****" + v["hint"]) if v.get("secret") and v.get("hint") else ("****" if v.get("secret") else None),
                         "value": None if v.get("secret") else v.get("value"), "updated_at": v.get("updated_at"),
                         "model": bool(v.get("model")), "disabled": bool(v.get("disabled"))}
                        for v in env.get("variables") or []]
    return env


def cmd_env_show(api: Api, args, out: Out):
    env = _env_view((api.portal("team_environment") or {}).get("team_environment") or {})
    _print_env(out, env)
    return env


def cmd_env_set(api: Api, args, out: Out):
    sources = [args.value is not None, args.value_stdin, args.from_env is not None]
    if sum(sources) > 1:
        raise CliError("usage", "use one of VALUE, --value-stdin, --from-env", EXIT_USAGE)
    if args.value_stdin:
        value = sys.stdin.read().rstrip("\n")
    elif args.from_env is not None:
        value = os.environ.get(args.from_env)
        if value is None:
            raise CliError("usage", "environment variable %s is not set" % args.from_env, EXIT_USAGE)
    elif args.value is not None:
        value = args.value
    elif interactive(args):
        value = getpass.getpass(out.t("Value (hidden): ", "变量值（不显示）："))
    else:
        raise CliError("usage", "give the value: --value-stdin (recommended for secrets), --from-env VAR or VALUE", EXIT_USAGE)
    env = _env_view((api.portal("save_team_variable", write=True, name=args.name, value=value, secret=not args.plain)
                     or {}).get("team_environment") or {})
    out.line(out.t("Saved %s.", "已保存 %s。") % args.name)
    return env


def _env_flags(api: Api, out: Out, name: str, **flags) -> dict:
    return _env_view((api.portal("set_team_variable_flags", write=True, name=name, **flags) or {}).get("team_environment") or {})


def cmd_env_disable(api: Api, args, out: Out):
    env = _env_flags(api, out, args.name, disabled=True)
    out.line(out.t("Switched off %s: kept, but not given to your program in evaluations.", "已停用 %s：保留，但评测时不提供给程序。") % args.name)
    return env


def cmd_env_enable(api: Api, args, out: Out):
    env = _env_flags(api, out, args.name, disabled=False)
    out.line(out.t("Switched on %s.", "已启用 %s。") % args.name)
    return env


def cmd_env_tag(api: Api, args, out: Out):
    env = _env_flags(api, out, args.name, model=args.tag == "model")
    out.line((out.t("%s is model-related: left out of evaluations without a model (eval start --no-model).",
                    "%s 标记为模型相关：在不提供模型的评测（eval start --no-model）中不提供。") if args.tag == "model" else
              out.t("%s is not model-related: given to every evaluation.", "%s 不再标记为模型相关：所有评测都会提供。")) % args.name)
    return env


def cmd_env_unset(api: Api, args, out: Out):
    env = _env_view((api.portal("delete_team_variable", write=True, name=args.name) or {}).get("team_environment") or {})
    out.line(out.t("Deleted %s.", "已删除 %s。") % args.name)
    return env


# Presets of the website's "Add a model service" form (web/src/lib/modelProviders.json; tests keep them equal).
MODEL_PROVIDERS = [
    {"cli": "kimi", "label": "Kimi Coding Plan", "protocol": "openai", "base_url": "https://api.kimi.com/coding/v1", "model": "kimi-for-coding"},
    {"cli": "moonshot", "label": "Moonshot (Kimi API)", "protocol": "openai", "base_url": "https://api.moonshot.cn/v1", "model": "kimi-k3"},
    {"cli": "deepseek", "label": "DeepSeek", "protocol": "openai", "base_url": "https://api.deepseek.com", "model": "deepseek-flash"},
    {"cli": "openai", "label": "OpenAI", "protocol": "openai", "base_url": "https://api.openai.com/v1", "model": ""},
    {"cli": "anthropic", "label": "Anthropic", "protocol": "anthropic", "base_url": "https://api.anthropic.com", "model": ""},
    {"cli": "zhipu", "label": "Zhipu GLM", "protocol": "openai", "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-5.3"},
    {"cli": "custom", "label": "Custom", "protocol": "openai", "base_url": "", "model": ""},
]


def _normalize_prefix(raw: str) -> str:
    """Same as the website: upper case, separators to _, no leading non-letters or trailing _; at most 40."""
    value = re.sub(r"[^A-Z0-9_]+", "_", raw.strip().upper())
    value = re.sub(r"^[^A-Z]+", "", value)
    return re.sub(r"_+$", "", value)[:40]


def cmd_env_model(api: Api, args, out: Out):
    preset = next(p for p in MODEL_PROVIDERS if p["cli"] == args.provider)
    prefix = _normalize_prefix(args.prefix or "")
    if args.prefix is not None and not prefix:
        raise CliError("usage", "--prefix needs a letter, e.g. KIMI", EXIT_USAGE)
    base = prefix or ("ANTHROPIC" if preset["protocol"] == "anthropic" else "OPENAI")
    names = {"key": base + "_API_KEY", "base_url": base + "_BASE_URL", "model": base + "_MODEL"}
    key = (sys.stdin.read() if args.key == "-" else args.key).strip()
    if not key:
        raise CliError("usage", "give the API key with --key KEY, or --key - to read it from standard input", EXIT_USAGE)
    base_url = (args.base_url if args.base_url is not None else preset["base_url"]).strip()
    if not re.match(r"^https://\S+$", base_url):
        raise CliError("usage", "--base-url must be an https:// address" + ("" if preset["base_url"] else " (required for --provider custom)"), EXIT_USAGE)
    model = (args.model if args.model is not None else preset["model"]).strip()
    env = (api.portal("team_environment") or {}).get("team_environment") or {}
    existing = [v.get("name") for v in env.get("variables") or []]
    writes = [(names["key"], key, True), (names["base_url"], base_url, False)] + ([(names["model"], model, False)] if model else [])
    deletes = [names["model"]] if not model and names["model"] in existing else []
    taken = [n for n, _, _ in writes if n in existing] + deletes
    if taken and not args.replace:
        raise CliError("variables_exist", out.t("Already set: %s. Pass --replace to overwrite them, or --prefix NAME to add another service.",
                                                "已存在：%s。如需覆盖请加 --replace，或用 --prefix 名称 添加另一个服务。") % ", ".join(taken))
    limit = (env.get("limits") or {}).get("variables")
    added = len([n for n, _, _ in writes if n not in existing])
    if limit is not None and len(existing) + added > limit:
        raise CliError("team_variable_limit")
    for name, value, secret in writes:
        env = api.portal("save_team_variable", write=True, name=name, value=value, secret=secret, model=True) or {}
    for name in deletes:
        env = api.portal("delete_team_variable", write=True, name=name) or {}
    out.line(out.t("Saved model service %s: %s (secret), %s%s.", "已保存模型服务 %s：%s（密文）、%s%s。")
             % (preset["label"], names["key"], names["base_url"], (out.t(", ", "、") + names["model"]) if model else ""))
    return {"provider": preset["cli"], "label": preset["label"], "protocol": preset["protocol"], "variables": names,
            "base_url": base_url, "model": model or None, "saved": [n for n, _, _ in writes], "deleted": deletes,
            "team_environment": _env_view(env.get("team_environment") or {})}


def cmd_env_domains(api: Api, args, out: Out):
    if args.domains_cmd in (None, "list"):
        env = _env_view((api.portal("team_environment") or {}).get("team_environment") or {})
        out.line(", ".join(env.get("domains") or []) or out.t("(none)", "（无）"))
        return {"domains": env.get("domains") or []}
    hosts = [] if args.domains_cmd == "clear" else args.hosts
    env = _env_view((api.portal("set_team_domains", write=True, domains=hosts) or {}).get("team_environment") or {})
    out.line(out.t("Allowed domains: ", "允许访问的域名：") + (", ".join(env.get("domains") or []) or out.t("(none)", "（无）")))
    return {"domains": env.get("domains") or []}


def cmd_env_route(api: Api, args, out: Out):
    env = None
    route = args.route
    if route is None:
        env = (api.portal("team_environment") or {}).get("team_environment") or {}
        route = (env.get("egress_route") or {}).get("route") or "direct"
    if args.route is not None or args.fallback is not None:
        fields = {"route": route} if args.fallback is None else {"route": route, "auto_fallback": args.fallback}
        env = (api.portal("set_team_egress_route", write=True, **fields) or {}).get("team_environment") or {}
    route = env.get("egress_route") or {"available": False, "route": "direct", "auto_fallback": True}
    out.line(_route_text(out, route))
    return {"egress_route": route}


# ---------------------------------------------------------------------------------------------
# Commands: projects and versions


def _revision_view(r: dict, batches: list) -> dict:
    return {
        "revision_id": r["id"], "title": r.get("title"), "status": "withdrawn" if r.get("archived_at") else r.get("status"),
        "raw_status": r.get("status"), "withdrawn": bool(r.get("archived_at")), "source_kind": r.get("source_kind"),
        "source_location": r.get("source_location"), "source_digest": r.get("source_digest"),
        "created_at": r.get("created_at"), "approved_at": r.get("approved_at"), "error": r.get("error") or "",
        "public_test": r.get("public_test") or {}, "evaluations": len([b for b in batches if b.get("revision_id") == r["id"]]),
    }


def cmd_project_list(api: Api, args, out: Out):
    data = portal_list(api)
    rows = [_revision_view(r, data.get("batches") or []) for r in all_revisions(data) if args.all or not r.get("archived_at")]
    out.table([dict(r, id=r["revision_id"][:8]) for r in rows],
              [("ID", "id"), (out.t("Project", "项目"), "title"), (out.t("Status", "状态"), "status"),
               (out.t("Source", "来源"), "source_kind"), (out.t("Evaluated", "已评测"), "evaluations"),
               (out.t("Created", "创建时间"), "created_at")])
    return rows


def _recent_duplicate(data: dict, title: str, url: str | None) -> bool:
    now = time.time()
    for r in all_revisions(data):
        created = _parse_time(r.get("created_at") or "")
        if created and now - created < 600 and r.get("title") == title and (url is None or r.get("source_location") == url):
            return True
    return False


def cmd_project_submit_repo(api: Api, args, out: Out):
    url = args.url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    title = (args.title or url.rsplit("/", 1)[-1]).strip()
    if _recent_duplicate(portal_list(api), title, url):
        confirm(args, out, "You submitted the same project a few minutes ago. Submit it again? This uses one of today’s project preparations.",
                "几分钟前刚提交过相同的项目。确定再提交一次吗？这会占用今天的 1 次项目准备机会。")
    # A branch/tag/commit or folder may also be part of the link (.../tree/<branch>/<folder>).
    options = {k: v.strip() for k, v in (("branch", args.branch), ("subdir", args.subdir)) if v and v.strip()}
    result = api.portal("submit_repository", write=True, title=title, url=url, **options) or {}
    if result.get("source_commit"):
        out.line(out.t("Saved commit %s%s.", "已记录 commit %s%s。") % (
            result["source_commit"][:12],
            "".join(f" · {result[k]}" for k in ("source_ref", "source_subdir") if result.get(k))))
    out.line(out.t("Project queued for preparation: version %s. Wait with: survey26 project wait %s",
                   "项目已排队，等待准备：版本 %s。可用 survey26 project wait %s 等待。") % (result.get("revision_id"), str(result.get("revision_id"))[:8]))
    return result


def cmd_project_upload(api: Api, args, out: Out):
    path = Path(args.zip)
    if not path.is_file():
        raise CliError("file_not_found")
    if not path.name.lower().endswith(".zip"):
        raise CliError("wrong_file_type")
    size = path.stat().st_size
    if not size or size > 50 * 1024 * 1024:
        raise CliError("file_too_large")
    if not zipfile.is_zipfile(str(path)):
        raise CliError("wrong_file_type", "not a ZIP archive")
    title = (args.title or path.stem).strip()
    if _recent_duplicate(portal_list(api), title, None):
        confirm(args, out, "You submitted the same project a few minutes ago. Submit it again? This uses one of today’s project preparations.",
                "几分钟前刚提交过相同的项目。确定再提交一次吗？这会占用今天的 1 次项目准备机会。")
    slot = api.portal("upload", write=True, purpose="source") or {}
    out.line(out.t("Uploading %s (%d bytes)…", "正在上传 %s（%d 字节）…") % (path.name, size))
    boundary = "----survey26" + os.urandom(8).hex()
    body = (("--%s\r\nContent-Disposition: form-data; name=\"cacheControl\"\r\n\r\n3600\r\n"
             "--%s\r\nContent-Disposition: form-data; name=\"\"; filename=\"%s\"\r\nContent-Type: application/zip\r\n\r\n")
            % (boundary, boundary, path.name.replace('"', ""))).encode() + path.read_bytes() + ("\r\n--%s--\r\n" % boundary).encode()
    http_put(slot["upload_url"], body, {"apikey": slot["apikey"], "Authorization": "Bearer " + slot["apikey"],
                                        "x-upsert": "false", "Content-Type": "multipart/form-data; boundary=" + boundary})
    result = None
    for attempt in range(5):
        try:
            result = api.portal("submit_zip", write=True, title=title, upload_id=slot["id"])
            break
        except CliError as error:
            if error.code != "upload_not_finished" or attempt == 4:
                raise
            _sleep(2 + attempt * 2)
    result = result or {}
    out.line(out.t("Project queued for preparation: version %s. Wait with: survey26 project wait %s",
                   "项目已排队，等待准备：版本 %s。可用 survey26 project wait %s 等待。") % (result.get("revision_id"), str(result.get("revision_id"))[:8]))
    return dict(result, upload_id=slot["id"], bytes=size)


def cmd_project_show(api: Api, args, out: Out):
    data = portal_list(api)
    r = find_revision(data, args.revision)
    view = _revision_view(r, data.get("batches") or [])
    view.update({"manifest": r.get("manifest"), "adapter_files": r.get("adapter_files") or {},
                 "explanation": r.get("explanation") or "", "approval_digest": r.get("approval_digest"),
                 "evidence": r.get("observer_evidence")})
    for key in ("revision_id", "title", "status", "source_kind", "source_location", "source_digest", "created_at", "approved_at"):
        out.line("%-16s %s" % (key, view.get(key) or ""))
    if view["error"]:
        out.line(out.t("Error: ", "错误：") + view["error"])
    if view["public_test"]:
        out.line(out.t("Public test: ", "公开场景测试：") + json.dumps(view["public_test"], ensure_ascii=False))
    if args.files or view["status"] == "reviewable":
        out.line()
        out.line(out.t("Execution settings:", "运行设置：") + "\n" + json.dumps(view["manifest"], ensure_ascii=False, indent=2))
        if view["explanation"]:
            out.line(out.t("Adapter explanation:", "适配说明：") + "\n" + view["explanation"])
        files = view["adapter_files"]
        if files:
            out.line(out.t("Added adapter files:", "新增的适配文件："))
            for name, content in files.items():
                out.line("--- %s\n%s" % (name, content if args.files else "(%d chars; add --files to print)" % len(content or "")))
        else:
            out.line(out.t("This project supplies its own interface; no adapter files were added.", "项目自带接口，没有新增适配文件。"))
    return view


def cmd_project_wait(api: Api, args, out: Out):
    deadline = time.time() + args.timeout
    last = None
    while True:
        data = polled_list(api, deadline)
        r = find_revision(data, args.revision)
        status = r.get("status")
        if status != last:
            out.line("%s  %s" % (time.strftime("%H:%M:%S"), status))
            if not out.json and status == "failed":
                out.line(r.get("error") or "")
            last = status
        if status not in REVISION_PENDING:
            view = _revision_view(r, data.get("batches") or [])
            if status == "failed":
                raise CliError("preparation_failed", r.get("error") or "preparation failed", EXIT_FAILED)
            return view
        if time.time() >= deadline:
            raise CliError("wait_timeout")
        _sleep(max(5, args.interval))


def cmd_project_logs(api: Api, args, out: Out):
    data = portal_list(api)
    r = find_revision(data, args.revision)
    diagnostics = api.portal("diagnostics", revision_id=r["id"]) or []
    test_run = (r.get("public_test") or {}).get("run_id")
    agent = None
    if test_run:
        try:
            agent = api.portal("agent_log", run_id=test_run, full=bool(args.full))
        except CliError:
            agent = None
    for d in diagnostics:
        out.line("== %s · %s · %s %s" % (d.get("kind"), d.get("status"), d.get("code") or "", d.get("finished_at") or ""))
        if d.get("log"):
            out.line(d["log"])
    if agent and agent.get("available"):
        out.line(out.t("== agent.log of the public test run", "== 公开测试运行的 agent.log"))
        out.line(agent.get("log") or "")
    return {"revision_id": r["id"], "diagnostics": diagnostics, "public_test_run_id": test_run, "agent_log": agent}


def cmd_project_confirm(api: Api, args, out: Out):
    data = portal_list(api)
    r = find_revision(data, args.revision)
    if r.get("status") != "reviewable" or r.get("archived_at"):
        raise CliError("revision_not_reviewable")
    confirm(args, out, "I reviewed the execution settings and adapter code (survey26 project show %s --files), and confirm this exact version." % r["id"][:8],
            "我已检查运行设置和适配代码（survey26 project show %s --files），确认使用这个版本。" % r["id"][:8])
    api.portal("approve", write=True, revision_id=r["id"], digest=r.get("approval_digest") or "")
    out.line(out.t("Version confirmed. Start an evaluation with: survey26 eval start %s", "已确认版本。可用 survey26 eval start %s 开始评测。") % r["id"][:8])
    return {"revision_id": r["id"], "confirmed": True}


def cmd_project_withdraw(api: Api, args, out: Out):
    data = portal_list(api)
    r = find_revision(data, args.revision)
    confirm(args, out, "Withdraw this version? It will be hidden and can no longer be confirmed or evaluated. The upload still counts toward today’s project preparations.",
            "撤回这个版本？撤回后它会被隐藏，不能再确认或评测；已用的上传次数不退回。")
    api.portal("withdraw", write=True, revision_id=r["id"])
    out.line(out.t("Version withdrawn.", "已撤回。"))
    return {"revision_id": r["id"], "withdrawn": True}


def cmd_project_download(api: Api, args, out: Out):
    data = portal_list(api)
    r = find_revision(data, args.revision)
    url = (api.portal("download_project", revision_id=r["id"]) or {}).get("url")
    target = Path(args.output or ("project-%s.zip" % r["id"][:8]))
    target.write_bytes(http_get(url))
    out.line(out.t("Saved ", "已保存 ") + str(target))
    return {"revision_id": r["id"], "path": str(target), "bytes": target.stat().st_size}


def cmd_project_evidence(api: Api, args, out: Out):
    data = portal_list(api)
    r = find_revision(data, args.revision)
    notes = sys.stdin.read() if args.notes == "-" else (args.notes or "")
    api.portal("evidence", write=True, revision_id=r["id"], notes=notes, code_url=args.code_url or "")
    out.line(out.t("Saved.", "已保存。"))
    return {"revision_id": r["id"], "saved": True}


# ---------------------------------------------------------------------------------------------
# Commands: evaluations and results


def _quota_for(data: dict, phase_id: str):
    for q in data.get("quota") or []:
        if q.get("phase_id") == phase_id:
            return q
    return None


EXTRA_NOTE = ("Unscored phase: it does not count for any leaderboard and has its own daily evaluations.",
              "不计分赛程：不计入任何排行榜，评测次数单独计算。")
NO_MODEL_NOTE = ("Without a model: your program gets none of the team variables marked model-related, and OBSERVER_MODEL_DISABLED=1.",
                 "本次不提供模型：程序拿不到标记为模型相关的队伍变量，并会收到 OBSERVER_MODEL_DISABLED=1。")


def cmd_eval_start(api: Api, args, out: Out):
    data = portal_list(api)
    r = find_revision(data, args.revision)
    phase = competition_phase(api, data, args.phase)
    quota = _quota_for(data, phase["phase_id"])
    counted = [b for b in data.get("batches") or [] if b.get("revision_id") == r["id"] and b.get("phase_id") == phase["phase_id"]
               and not b.get("quota_refunded")]
    repeat = bool(counted)
    if repeat:
        left = ("（今天还剩 %s 次）" % quota["remaining"]) if quota else ""
        left_en = (" (%s left today)" % quota["remaining"]) if quota else ""
        confirm(args, out, "This version has already been evaluated. Evaluating it again uses one more of today’s evaluations%s. Continue?" % left_en,
                "这个版本已经评测过。再评测一次会再占用今天 1 次评测%s。确定继续吗？" % left)
    fields = {"phase_id": phase["phase_id"], "revision_id": r["id"], **({"no_model": True} if args.no_model else {})}
    try:
        result = api.portal("evaluate", write=True, **dict(fields, confirm_repeat=True) if repeat else fields)
    except CliError as error:
        if repeat or error.code != "revision_already_evaluated":
            raise
        confirm(args, out, "This version has already been evaluated. Evaluate it again?", "这个版本已经评测过。确定再评测一次吗？")
        result = api.portal("evaluate", write=True, confirm_repeat=True, **fields)
    batch_id = (result or {}).get("batch_id")
    out.line(out.t("Evaluation queued: %s (phase %s). Wait with: survey26 eval wait %s",
                   "已加入评测队列：%s（赛程 %s）。可用 survey26 eval wait %s 等待。") % (batch_id, phase["slug"], str(batch_id)[:8]))
    if phase["extra"]:
        out.line(EXTRA_NOTE[out.lang == "zh"])
    if args.no_model:
        out.line(NO_MODEL_NOTE[out.lang == "zh"])
    return {"batch_id": batch_id, "phase_id": phase["phase_id"], "phase": phase["slug"], "revision_id": r["id"], "repeat": repeat,
            "model_disabled": bool(args.no_model), "extra": phase["extra"]}


def cmd_eval_selfcheck(api: Api, args, out: Out):
    data = portal_list(api)
    r = find_revision(data, args.revision)
    phase = competition_phase(api, data, args.phase)
    quota = _quota_for(data, phase["phase_id"])
    if quota and quota.get("remaining") is not None and quota["remaining"] < SELF_CHECK_RUNS:
        raise CliError("repeat_daily_limit")
    remaining = quota.get("remaining") if quota else "?"
    confirm(args, out, "Evaluate this version 3 times in a row? This uses 3 of today’s evaluations (%s left today)." % remaining,
            "将对此版本连续评测 3 次，占用今天 3 次评测（今天还剩 %s 次）。确定继续吗？" % remaining)
    result = api.rpc("observer_create_repeat_batches", write=True, p_phase=phase["phase_id"], p_revision=r["id"], p_confirm_repeat=True,
                     **({"p_no_model": True} if args.no_model else {}))
    out.line(out.t("Queued: the 3 evaluations run one after another.", "已加入评测队列，3 次评测将依次进行。"))
    if phase["extra"]:
        out.line(EXTRA_NOTE[out.lang == "zh"])
    if args.no_model:
        out.line(NO_MODEL_NOTE[out.lang == "zh"])
    return {"result": result, "phase_id": phase["phase_id"], "phase": phase["slug"], "revision_id": r["id"],
            "model_disabled": bool(args.no_model), "extra": phase["extra"]}


def cmd_eval_list(api: Api, args, out: Out):
    data = portal_list(api)
    data = in_phase(data, phase_filter(api, data, args.phase))
    names = scenario_names(api, data)
    slugs = phase_slugs(data)
    rows = [batch_summary(b, names, out.lang, slugs) for b in (data.get("batches") or [])[:args.limit]]
    titles = {r["id"]: r.get("title") for r in all_revisions(data)}
    out.table([dict(b, id=b["batch_id"][:8], version=(b["revision_id"] or "")[:8], project=titles.get(b["revision_id"]),
                    score_text=fmt_score(b["score"]), self_check="3x" if b["repeat_group"] else "",
                    no_model=out.t("no model", "无模型") if b["model_disabled"] else "",
                    counted="" if not b["quota_refunded"] else out.t("not counted", "未计次")) for b in rows],
              [("ID", "id"), (out.t("Phase", "赛程"), "phase"), (out.t("Status", "状态"), "status"), (out.t("Score", "分数"), "score_text"),
               (out.t("Version", "版本"), "version"), (out.t("Project", "项目"), "project"), ("", "self_check"), ("", "no_model"),
               ("", "counted"), (out.t("Created", "创建时间"), "created_at")])
    return rows


def _repeat_summary(data: dict, group: str) -> dict | None:
    own = [b for b in data.get("batches") or [] if b.get("repeat_group") == group]
    if not own:
        return None
    scored = [b for b in own if b.get("status") == "scored"]

    def spread(values):
        return None if not values else {"mean": sum(values) / len(values), "min": min(values), "max": max(values)}
    cards: dict = {}
    for b in scored:
        for run in b.get("observer_runs") or []:
            if run.get("score") is not None:
                cards.setdefault(run["scenario_id"], []).append(float(run["score"]))
    return {"group": group, "runs": own[0].get("repeat_runs") or SELF_CHECK_RUNS, "scored": len(scored),
            "active": len([b for b in own if b.get("status") in ACTIVE]),
            "cards": [dict(spread(v), scenario_id=k) for k, v in cards.items()],
            "overall": spread([float(b["score"]) for b in scored if b.get("score") is not None])}


def _show_batch(api: Api, data: dict, batch: dict, out: Out) -> dict:
    names = scenario_names(api, data)
    summary = batch_summary(batch, names, out.lang, phase_slugs(data))
    out.line("%s  %s  %s %s" % (summary["batch_id"], summary["status"], out.t("score", "分数"), fmt_score(summary["score"]))
             + (out.t("  (no model)", "  （无模型）") if summary["model_disabled"] else ""))
    out.table([dict(r, score_text=fmt_score(r["score"]), result=out.t("yes", "有") if r["has_result"] else "") for r in summary["runs"]],
              [(out.t("Card", "任务卡"), "label"), (out.t("Status", "状态"), "status"), (out.t("Score", "分数"), "score_text"),
               (out.t("Run ID", "运行 ID"), "run_id"), (out.t("Result", "结果"), "result")])
    if summary["repeat_group"]:
        summary["self_check"] = _repeat_summary(data, summary["repeat_group"])
        sc = summary["self_check"]
        if sc and sc["overall"]:
            out.line(out.t("Self-check (%d/%d scored): mean %.2f (%.2f–%.2f)", "自检（已完成 %d/%d 次）：平均 %.2f（%.2f–%.2f）")
                     % (sc["scored"], sc["runs"], sc["overall"]["mean"], sc["overall"]["min"], sc["overall"]["max"]))
    return summary


def cmd_eval_show(api: Api, args, out: Out):
    data = portal_list(api)
    return _show_batch(api, data, find_batch(in_phase(data, phase_filter(api, data, args.phase)), args.batch), out)


def cmd_eval_wait(api: Api, args, out: Out):
    deadline = time.time() + args.timeout
    last = None
    batch_id = None
    phase_id = None
    while True:
        data = polled_list(api, deadline)
        if batch_id is None:
            phase_id = phase_filter(api, data, args.phase)
        batch = find_batch(in_phase(data, phase_id), batch_id or args.batch)
        batch_id = batch["id"]
        state = (batch.get("status"), tuple((r.get("status"), r.get("score")) for r in batch.get("observer_runs") or []))
        if state != last:
            done = len([r for r in batch.get("observer_runs") or [] if r.get("status") in FINISHED_RUN])
            out.line("%s  %s  %d/%d %s" % (time.strftime("%H:%M:%S"), batch.get("status"), done, len(batch.get("observer_runs") or []),
                                           out.t("cards finished", "张卡已结束")))
            last = state
        if batch.get("status") not in ACTIVE:
            summary = _show_batch(api, data, batch, out)
            if batch.get("status") != "scored":
                raise CliError("evaluation_" + str(batch.get("status")), out.t("The evaluation ended with status %s. See survey26 results log RUN_ID.",
                                                                               "评测结束，状态为 %s。可用 survey26 results log 运行ID 查看日志。") % batch.get("status"), EXIT_FAILED)
            return summary
        if time.time() >= deadline:
            raise CliError("wait_timeout")
        _sleep(max(5, args.interval))


def cmd_results_log(api: Api, args, out: Out):
    data = portal_list(api)
    _, run = find_run(data, args.run)
    diagnostics = []
    if not args.agent_only:
        try:
            diagnostics = api.portal("diagnostics", run_id=run["id"]) or []
        except CliError:
            diagnostics = []
    view = api.portal("agent_log", run_id=run["id"], full=bool(args.full or args.output)) or {}
    text = view.get("log") or ""
    if args.tail and not args.output:
        text = "\n".join(text.splitlines()[-args.tail:])
    if args.output:
        Path(args.output).write_text(view.get("log") or "", encoding="utf-8")
        out.line(out.t("Saved ", "已保存 ") + args.output)
    else:
        for d in diagnostics:
            out.line("== %s · %s · %s" % (d.get("kind"), d.get("status"), d.get("code") or ""))
            if d.get("log"):
                out.line(d["log"])
        if view.get("available"):
            out.line("== agent.log" + (out.t(" (last part; --full for all)", "（末尾部分；--full 查看全部）") if view.get("truncated") else ""))
            out.line(text)
        else:
            out.line(out.t("agent.log is not available for this run; download the result ZIP instead.",
                           "这次运行没有可读取的 agent.log，可下载结果 ZIP 查看。"))
    result = {"run_id": run["id"], "diagnostics": diagnostics, "available": bool(view.get("available")),
              "bytes": view.get("bytes"), "truncated": bool(view.get("truncated")) and not (args.full or args.output)}
    if args.output:
        result["path"] = args.output
    else:
        result["log"] = text
    return result


def _download_run(api: Api, run_id: str) -> bytes:
    def once():
        url = (api.portal("download_result", run_id=run_id) or {}).get("url")
        if not url or not (url.startswith("https://") or url.startswith("http://127.0.0.1")):
            raise CliError("download_failed", "invalid download address")
        return http_get(url)
    try:
        return once()
    except CliError as error:
        if error.code in NOT_FOUND_CODES:
            raise
        return once()  # one more try with a fresh signed address, like the website


def cmd_results_download(api: Api, args, out: Out):
    data = portal_list(api)
    batch, run = find_run(data, args.run)
    names = scenario_names(api, data) if batch else {}
    slug = (names.get(run.get("scenario_id")) or {}).get("slug") or ""
    content = _download_run(api, run["id"])
    target = Path(args.output or ("result-%s-%s.zip" % (card_folder_name(slug, "card"), run["id"][:8])))
    target.write_bytes(content)
    out.line(out.t("Saved ", "已保存 ") + str(target))
    return {"run_id": run["id"], "card": slug or None, "path": str(target), "bytes": len(content)}


def evaluation_metadata(batch: dict, version) -> dict:
    """evaluation.json in a combined download (same fields as the website's)."""
    off = bool(batch.get("model_disabled"))
    meta = {"evaluation_id": batch.get("id"), "created_at": batch.get("created_at"), "phase_id": batch.get("phase_id"),
            "revision_id": batch.get("revision_id"), "version": version, "model_provided": not off, "model_disabled": off}
    if batch.get("repeat_group"):
        meta["self_check_group"] = batch["repeat_group"]
    return meta


def cmd_results_download_all(api: Api, args, out: Out):
    data = portal_list(api)
    batch = find_batch(in_phase(data, phase_filter(api, data, args.phase)), args.batch)
    names = scenario_names(api, data)
    runs = [r for r in batch.get("observer_runs") or [] if r.get("result_path")]
    runs.sort(key=lambda r: scenario_order((names.get(r.get("scenario_id")) or {}).get("slug") or ""))
    if not runs:
        raise CliError("result_not_ready")
    files: dict = {}
    errors = []
    cards = []
    for index, run in enumerate(runs):
        folder = ordered_card_folder(index, len(runs), card_folder_name((names.get(run.get("scenario_id")) or {}).get("slug") or "", run["id"]))
        try:
            with zipfile.ZipFile(io.BytesIO(_download_run(api, run["id"]))) as z:
                entries = {n: z.read(n) for n in z.namelist() if not n.endswith("/")}
            for name, content in flatten_result_entries(entries).items():
                files[folder + "/" + name] = content
            cards.append({"run_id": run["id"], "folder": folder, "ok": True})
        except (CliError, zipfile.BadZipFile) as error:
            errors.append("%s: %s" % (folder, getattr(error, "code", "bad_zip")))
            cards.append({"run_id": run["id"], "folder": folder, "ok": False})
        out.line(out.t("Downloaded %d/%d", "已下载 %d/%d") % (index + 1, len(runs)))
    if not files:
        raise CliError("download_failed", out.t("Could not download any card’s result.", "所有卡片的结果都下载失败。"))
    if errors:
        files["errors.txt"] = ("\n".join(errors) + "\n").encode("utf-8")
    titles = {r["id"]: r.get("title") for r in all_revisions(data)}
    files["evaluation.json"] = (json.dumps(evaluation_metadata(batch, titles.get(batch.get("revision_id"))), indent=2, ensure_ascii=False)
                                + "\n").encode("utf-8")
    no_model = bool(batch.get("model_disabled"))
    target = Path(args.output or ("results-%s%s.zip" % (batch["id"][:8], "-no-model" if no_model else "")))
    with zipfile.ZipFile(str(target), "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    out.line(out.t("Saved ", "已保存 ") + str(target) + ("" if not errors else out.t(" (some cards failed; see errors.txt)", "（部分卡片失败，详见 errors.txt）")))
    return {"batch_id": batch["id"], "path": str(target), "cards": cards, "errors": errors}


# ---------------------------------------------------------------------------------------------
# Commands: final version, quota, leaderboard, competition, Kimi plan, credits


def _final_for(data: dict, api: Api, phase_hint: str | None = None):
    finals = data.get("final_versions") or []
    if phase_hint:
        for f in finals:
            if f.get("phase_id") == phase_hint:
                return f
    return finals[0] if finals else None


def cmd_final_show(api: Api, args, out: Out):
    data = portal_list(api)
    final = _final_for(data, api)
    if not final:
        out.line(message_for("no_team_version", out.lang))
        return None
    titles = {r["id"]: r.get("title") for r in all_revisions(data)}
    out.line(out.t("Final version: ", "最终版本：") + "%s (%s) %s" % (final.get("revision_id") or "-", titles.get(final.get("revision_id")) or "",
                                                                    {"chosen": out.t("chosen by your team", "本队已选择"), "best": out.t("default: best evaluation", "默认：最高分评测")}.get(final.get("source"), "")))
    out.line(out.t("Changeable until: ", "可修改至：") + str(final.get("deadline")) + (out.t(" (locked)", "（已锁定）") if final.get("locked") else ""))
    return final


def cmd_final_set(api: Api, args, out: Out):
    data = portal_list(api)
    final = _final_for(data, api)
    if not final:
        raise CliError("no_team_version")
    r = find_revision(data, args.revision)
    result = api.portal("set_final_version", write=True, phase_id=final["phase_id"], revision_id=r["id"])
    out.line(out.t("Final version saved.", "已保存最终版本。"))
    return (result or {}).get("final_version") or result


def cmd_final_clear(api: Api, args, out: Out):
    data = portal_list(api)
    final = _final_for(data, api)
    if not final:
        raise CliError("no_team_version")
    confirm(args, out, "Clear your choice? The version of your best evaluation will be used instead.", "取消选择？将改用本队最高分评测的版本。")
    result = api.portal("set_final_version", write=True, phase_id=final["phase_id"], revision_id=None)
    out.line(out.t("Choice cleared; the default applies.", "已取消选择，恢复默认。"))
    return (result or {}).get("final_version") or result


def cmd_quota(api: Api, args, out: Out):
    data = portal_list(api)
    extra = extra_phase_id(api)
    phases = {p["phase_id"]: (p.get("phases") or {}) for p in data.get("phases") or []}
    rows = [dict(q, phase=phases.get(q.get("phase_id"), {}).get("slug"), name_en=phases.get(q.get("phase_id"), {}).get("name_en"),
                 name_zh=phases.get(q.get("phase_id"), {}).get("name_zh"), extra=bool(extra) and q.get("phase_id") == extra)
            for q in data.get("quota") or []]
    out.table([dict(q, name=q["name_zh" if out.lang == "zh" else "name_en"], note=out.t("unscored", "不计分") if q["extra"] else "")
               for q in rows],
              [(out.t("Phase", "赛程"), "phase"), (out.t("Name", "名称"), "name"), ("", "note"), (out.t("Per day", "每天"), "daily_batches"), (out.t("Used", "已用"), "used"),
                     (out.t("Left", "剩余"), "remaining"), (out.t("Preparations/day", "每天可准备"), "preparations_daily"),
                     (out.t("Preparations left", "剩余准备"), "preparations_remaining"), (out.t("Resets", "重置时间"), "resets_at")])
    return rows


def cmd_competition(api: Api, args, out: Out):
    comp = api.rpc("current_competition") or {}
    phases = api.call("phases") or []
    by_id = {p["id"]: p for p in phases}
    extra_id = comp.get("extra_phase_id") or None
    view = {"mode": comp.get("mode"), "phase": by_id.get(comp.get("phase_id")), "project_phase": by_id.get(comp.get("project_phase_id")),
            "extra_phase": by_id.get(extra_id) if extra_id else None,
            "phases": [p for p in phases if p.get("slug") in ("practice-projects", "practice", "online", "final-hidden")
                       or (extra_id and p.get("id") == extra_id)]}
    out.line(out.t("Mode: ", "模式：") + str(view["mode"]))
    out.table([dict(p, name=p.get("name_zh" if out.lang == "zh" else "name_en"),
                    note=out.t("unscored", "不计分") if extra_id and p.get("id") == extra_id else "") for p in view["phases"]],
              [("slug", "slug"), (out.t("Name", "名称"), "name"), ("", "note"), (out.t("Starts", "开始"), "starts_at"),
               (out.t("Ends", "结束"), "ends_at"), (out.t("Active", "启用"), "is_active")])
    extra = view["extra_phase"]
    if extra:
        name = extra.get("name_zh" if out.lang == "zh" else "name_en") or extra.get("slug")
        out.line(out.t("Extra phase: %s (unscored, not on any leaderboard, own daily evaluations). Evaluate there with --phase %s (or --phase extra).",
                       "额外赛程：%s（不计分，不上任何排行榜，评测次数单独计算）。在该赛程评测请加 --phase %s（或 --phase extra）。")
                 % (name, extra.get("slug")))
    return view


def cmd_leaderboard(api: Api, args, out: Out):
    phases = api.call("phases") or []
    me = _me(api) if args.mine else {}
    team_id = (me.get("team") or {}).get("id")
    slug = args.phase
    if not slug:
        comp = api.rpc("current_competition") or {}
        slug = "online" if comp.get("mode") == "competition" else "practice-projects"
    phase = next((p for p in phases if p.get("slug") == slug), None)
    if not phase:
        raise CliError("not_found", "no such leaderboard: %s" % slug, EXIT_NOT_FOUND)
    settings = phase.get("observer_settings") or {}
    if isinstance(settings, list):
        settings = settings[0] if settings else {}
    card = None
    baselines: list = []
    if settings.get("projects_enabled"):
        board = api.rpc("observer_card_board", p_phase=phase["id"], p_scenario_slug=args.card, p_limit=args.limit) or {}
        rows = board.get("rows") if isinstance(board, dict) else board
        card = board.get("scenario") if isinstance(board, dict) else None
        cards = board.get("cards") if isinstance(board, dict) else None
        if slug == "online":
            baselines = baseline_rows(api, phase["id"])
    else:
        rows = api.rpc("leaderboard", p_phase_slug=slug, p_limit=args.limit, p_scenario_slug=args.card)
        cards = None
    rows = rows or []
    mine = [r for r in rows if team_id and r.get("team_id") == team_id]
    shown = mine if args.mine else rows
    table = [dict(r, score_text=fmt_score(r.get("total_score"))) for r in shown]
    placed = 0
    if not args.mine:
        table, placed = with_baselines(table, baselines, card, out)
    out.table(table, [("#", "rank"), (out.t("Team", "队伍"), "team_name"), (out.t("Score", "分数"), "score_text"),
                      (out.t("Evaluations", "评测次数"), "submission_count")])
    if placed:
        out.line(out.t("Baseline: average score of the official examples run unmodified (with the organizers' model key). For reference only; not ranked.",
                       "基线：官方示例原样运行的平均分（使用组委会的模型 key），仅供参考，不参与排名。"))
    listed = sorted([c.get("slug") for c in cards or [] if isinstance(c, dict) and c.get("slug")], key=scenario_order)
    if listed and not args.mine:
        out.line(out.t("Cards (--card): ", "任务卡（--card）：") + ", ".join(listed))
    if args.mine and not mine:
        out.line(out.t("Your team is not on this board yet.", "本队尚未出现在这个排行榜上。"))
    return {"phase": slug, "card": card, "cards": cards, "rows": shown, "baselines": baselines, "my_team_id": team_id}


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def baseline_rows(api: Api, phase_id: str) -> list:
    """The online board's unranked reference rows (official examples' averages, basic and pro); none on any error."""
    try:
        data = api.rpc("observer_baseline_rows", p_phase=phase_id)
    except CliError:
        return []
    return [{"group": r["group"], "overall_score": r["overall_score"],
             "card_scores": r.get("card_scores") if isinstance(r.get("card_scores"), dict) else None,
             "runs": r.get("runs"), "updated_at": r.get("updated_at")}
            for r in (data if isinstance(data, list) else [])
            if isinstance(r, dict) and r.get("group") in ("basic", "pro") and _number(r.get("overall_score"))]


BASELINE_NAMES = {"basic": ("Baseline · official examples (basic)", "基线 · 官方示例（普通版）"),
                  "pro": ("Baseline · official examples (pro)", "基线 · 官方示例（pro 版）")}


def with_baselines(table: list, baselines: list, tab, out: Out) -> tuple:
    """As on the website: each baseline sits after every team scoring at least as much; ranks stay unchanged."""
    refs = []
    for b in baselines:
        score = b["overall_score"] if tab is None else (b["card_scores"] or {}).get(tab)
        if _number(score):
            refs.append((b, score))
    refs.sort(key=lambda x: -x[1])
    rows = list(table)
    for b, score in refs:
        at = next((i for i, r in enumerate(rows) if not r.get("baseline")
                   and (not _number(r.get("total_score")) or r["total_score"] < score)), len(rows))
        rows.insert(at, {"baseline": b["group"], "rank": "—", "team_name": out.t(*BASELINE_NAMES[b["group"]]),
                         "score_text": fmt_score(score), "submission_count": b.get("runs")})
    return rows, len(refs)


RELAY_BASE = "https://vdiemcofukuxglqsmlyz.supabase.co/functions/v1/kimi-relay/v1"
RELAY_MODEL = "kimi-for-coding"


def cmd_relay_status(api: Api, args, out: Out):
    relay = api.rpc("my_kimi_relay") or {}
    relay = relay if isinstance(relay, dict) else {}

    def left(limit, used):
        return max(0, limit - (used if _number(used) else 0)) if _number(limit) else None
    view = {"enabled": bool(relay.get("enabled")), "has_team": bool(relay.get("has_team")), "eligible": bool(relay.get("eligible")),
            "base_url": RELAY_BASE, "model": RELAY_MODEL,
            "remaining_requests": left(relay.get("daily_requests"), relay.get("used_requests")),
            "remaining_tokens": left(relay.get("daily_tokens"), relay.get("used_tokens")),
            **{k: relay.get(k) for k in ("daily_requests", "daily_tokens", "used_requests", "used_tokens", "max_concurrent", "max_tokens")}}
    if not view["enabled"]:
        out.line(out.t("The temporary Kimi relay is not available right now.", "平台临时 Kimi 中转目前未开放。"))
    elif not view["has_team"]:
        out.line(message_for("need_team", out.lang))
    elif not view["eligible"]:
        out.line(out.t("Available once your team is on the leaderboard (one scored formal evaluation in the online phase).",
                       "上榜后即可使用（正式赛有一次成功评测）。"))
    else:
        out.line(out.t("Base URL: ", "接口地址：") + RELAY_BASE)
        out.line(out.t("Model: ", "模型：") + RELAY_MODEL + out.t("   API key: your personal API token (s26_...)", "   API key：你的个人 API 令牌（s26_...）"))
        out.line(out.t("Your team's allowance today: %s / %s requests, %s / %s tokens", "本队今天剩余：%s / %s 次请求，%s / %s tokens")
                 % (view["remaining_requests"], view["daily_requests"], view["remaining_tokens"], view["daily_tokens"]))
        out.line(out.t("Up to %s concurrent requests per team; max_tokens is capped at %s. Resets daily at 00:00 UTC.",
                       "每队最多同时 %s 个请求；max_tokens 上限 %s。每天 UTC 0 点（北京时间 8 点）重置。")
                 % (view["max_concurrent"], view["max_tokens"]))
    out.line(out.t("For local development only; evaluations use the model service saved under Keys and network (survey26 env).",
                   "仅供本地开发调试；正式评测使用「密钥与网络」中保存的模型服务（survey26 env）。"))
    return view


def cmd_kimi_status(api: Api, args, out: Out):
    status = api.rpc("kimi_plan_status") or {}
    for key in ("has_team", "is_captain", "qualified", "eligible", "imported", "available", "code", "claimed_at"):
        out.line("%-12s %s" % (key, status.get(key)))
    return status


def cmd_kimi_claim(api: Api, args, out: Out):
    result = api.rpc("claim_kimi_plan_code", write=True) or {}
    out.line(out.t("Your team already claimed its Kimi Coding Plan code." if result.get("already") else "Kimi Coding Plan code claimed.",
                   "你的队伍已领取过 Kimi Coding Plan 兑换码。" if result.get("already") else "已领取 Kimi Coding Plan 兑换码。"))
    status = api.rpc("kimi_plan_status") or {}
    out.line(str(status.get("code") or ""))
    return status


def cmd_credits_list(api: Api, args, out: Out):
    providers = api.rpc("redeem_providers") or []
    codes = api.rpc("my_redeem_codes") or []
    out.table(providers, [(out.t("Provider", "提供方"), "provider"), (out.t("Available", "剩余"), "available"),
                          (out.t("Claimed by your team", "本队已领取"), "claimed_by_my_team")])
    for c in codes:
        out.line("%s: %s %s" % (c.get("provider"), c.get("code"), c.get("note") or ""))
    return {"providers": providers, "codes": codes}


def cmd_credits_claim(api: Api, args, out: Out):
    result = api.rpc("claim_redeem_code", write=True, p_provider=args.provider) or {}
    out.line("%s: %s %s" % (result.get("provider"), result.get("code"), result.get("note") or ""))
    return result


# ---------------------------------------------------------------------------------------------
# Argument parsing


PHASE_HELP = "a phase slug (online, practice-projects, ...) or 'extra' (the optional unscored phase)"


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print one JSON object (stable schema) instead of text")
    common.add_argument("--token", default=argparse.SUPPRESS, help="API token (default: $SURVEY26_TOKEN, then the saved login)")
    common.add_argument("--lang", choices=["en", "zh"], default=argparse.SUPPRESS, help="message language (default: $SURVEY26_LANG or $LANG)")
    common.add_argument("--api", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p = argparse.ArgumentParser(
        prog="survey26", parents=[common],
        description="Official command-line tool of the GOSIM 2026 Agentic Observer Hackathon: everything the website "
                    "(https://create.gosim.org/survey26/) lets a contestant do. Create a personal API token on your profile page first.",
        epilog="Exit codes: 0 ok, 1 refused, 2 usage/needs --yes, 3 auth, 4 not found, 5 rate limited, 6 unavailable, "
               "7 wait timeout, 8 limit reached, 9 preparation/evaluation failed. Guide: " + SITE + "/cli")
    p.add_argument("--version", action="version", version="survey26 " + __version__)
    sub = p.add_subparsers(dest="group", metavar="COMMAND")

    def add(parent, name, func, help_text, **kw):
        sp = parent.add_parser(name, parents=[common], help=help_text, description=help_text, **kw)
        sp.set_defaults(func=func)
        return sp

    def yes(sp):
        sp.add_argument("-y", "--yes", action="store_true", help="confirm without asking (needed with --json or without a terminal)")
        return sp

    login = add(sub, "login", cmd_login, "save an API token on this computer (checks it first)")
    login.add_argument("--token-stdin", action="store_true", help="read the token from standard input")
    add(sub, "logout", cmd_logout, "remove the saved token from this computer")
    add(sub, "whoami", cmd_whoami, "show the account and team the token acts as")

    prof = sub.add_parser("profile", help="your profile and avatar").add_subparsers(dest="cmd", metavar="ACTION")
    add(prof, "show", cmd_profile_show, "show your profile")
    ps = add(prof, "set", cmd_profile_set, "change profile fields")
    for name in PROFILE_OPTIONS:
        ps.add_argument("--" + name)
    ps.add_argument("--astro-level", type=int, choices=range(0, 6))
    ps.add_argument("--ai-level", type=int, choices=range(0, 6))
    av = prof.add_parser("avatar", help="avatar image").add_subparsers(dest="avatar_cmd", metavar="ACTION")
    add(av, "set", cmd_avatar_set, "upload a PNG, JPG or WEBP (at most 2 MB; use a square image)").add_argument("file")
    add(av, "clear", cmd_avatar_clear, "remove your avatar")

    tm = sub.add_parser("teammates", help="Find teammates page").add_subparsers(dest="cmd", metavar="ACTION")
    tl = add(tm, "list", cmd_teammates_list, "people listed on Find teammates")
    tl.add_argument("--limit", type=int, default=200)
    tl.add_argument("--looking", action="store_true", help="only people looking for a team or teammates")
    add(tm, "contact", cmd_teammates_contact, "show someone's contact details (as on the page)").add_argument("user_id")
    add(tm, "invite", cmd_teammates_invite, "invite someone to your team").add_argument("user_id")
    tv = add(tm, "visibility", cmd_teammates_visibility, "show, or switch on/off, your listing on Find teammates")
    tv.add_argument("state", choices=["show", "on", "off"])
    tv.add_argument("--blurb")
    tv.add_argument("--contact")
    tv.add_argument("--seeking", choices=["", "astro", "ai"], help="the teammate you look for: astro (astronomy), ai, or '' (none)")
    tv.add_argument("--seeking-count", type=int, choices=[1, 2])

    team = sub.add_parser("team", help="create, join and manage your team").add_subparsers(dest="cmd", metavar="ACTION")
    add(team, "show", cmd_team_show, "your team, invite code and members")
    add(team, "members", cmd_team_members, "list members")
    tc = add(team, "create", cmd_team_create, "create a team (you become captain)")
    tc.add_argument("name")
    tc.add_argument("--max-size", type=int, default=3, choices=[1, 2, 3])
    tc.add_argument("--repo", help="GitHub repository (optional)")
    tc.add_argument("--idea", help="project idea (optional)")
    add(team, "join", cmd_team_join, "join with an invite code").add_argument("code")
    yes(add(team, "leave", cmd_team_leave, "leave your team"))
    tset = add(team, "set", cmd_team_set, "captain: rename, size, repository, idea, lock")
    tset.add_argument("--name")
    tset.add_argument("--max-size", type=int, choices=[1, 2, 3])
    tset.add_argument("--repo")
    tset.add_argument("--idea")
    tset.add_argument("--lock", dest="lock", action="store_true", default=None)
    tset.add_argument("--unlock", dest="lock", action="store_false")
    add(team, "code", cmd_team_code, "show the invite code and link").add_argument("--regenerate", action="store_true", help="captain: make a new code (the old one stops working)")
    yes(add(team, "transfer", cmd_team_transfer, "captain: make another member captain")).add_argument("user_id")
    yes(add(team, "kick", cmd_team_kick, "captain: remove a member")).add_argument("user_id")
    yes(add(team, "disband", cmd_team_disband, "captain: disband the team"))
    add(team, "directory", cmd_team_directory, "teams that accept join requests")
    add(team, "request", cmd_team_request, "ask a team's captain to let you join").add_argument("team_id")
    add(team, "capacity", cmd_team_capacity, "remaining team places")
    add(team, "invite-uid", cmd_team_invite_uid, "captain: invite the person with this UID (9 digits) to your team").add_argument("uid")

    inv = sub.add_parser("invites", help="team invitations and join requests").add_subparsers(dest="invites_cmd", metavar="ACTION")
    add(inv, "list", cmd_invites_list, "list invitations and requests (marks received ones read)").add_argument("--keep-unread", action="store_true")
    add(inv, "accept", cmd_invites_respond, "accept an invitation or join request").add_argument("invitation_id")
    add(inv, "decline", cmd_invites_respond, "decline an invitation or join request").add_argument("invitation_id")
    add(inv, "cancel", cmd_invites_cancel, "withdraw an invitation or request you sent").add_argument("invitation_id")

    fr = sub.add_parser("friends", help="friends, friend requests and your UID").add_subparsers(dest="cmd", metavar="ACTION")
    add(fr, "list", cmd_friends_list, "your UID, friends, pending requests and blocked people")
    add(fr, "add", cmd_friends_add, "send a friend request to a UID (at most 20 per day)").add_argument("uid")
    add(fr, "accept", cmd_friends_respond, "accept a friend request").add_argument("request_id")
    add(fr, "decline", cmd_friends_respond, "decline a friend request").add_argument("request_id")
    add(fr, "cancel", cmd_friends_cancel, "withdraw a friend request you sent").add_argument("request_id")
    add(fr, "remove", cmd_friends_remove, "remove a friend").add_argument("user_id")
    add(fr, "block", cmd_friends_block, "block someone: their requests are no longer shown to you").add_argument("user_id")
    add(fr, "unblock", cmd_friends_block, "unblock someone").add_argument("user_id")

    env = sub.add_parser("env", help="team variables (secrets) and allowed domains").add_subparsers(dest="cmd", metavar="ACTION")
    add(env, "show", cmd_env_show, "list variables (secret values masked to the last 4 characters) and domains")
    es = add(env, "set", cmd_env_set, "create or replace a variable (secret unless --plain)")
    es.add_argument("name")
    es.add_argument("value", nargs="?", help="the value (visible in shell history; prefer --value-stdin)")
    es.add_argument("--value-stdin", action="store_true")
    es.add_argument("--from-env", metavar="VAR", help="take the value from this environment variable")
    es.add_argument("--plain", action="store_true", help="not secret: the value stays readable")
    add(env, "unset", cmd_env_unset, "delete a variable").add_argument("name")
    add(env, "disable", cmd_env_disable, "switch a variable off: kept (secrets stay encrypted) but not given to evaluations").add_argument("name")
    add(env, "enable", cmd_env_enable, "switch a variable on again").add_argument("name")
    et = add(env, "tag", cmd_env_tag, "mark a variable model-related or not (model-related ones are left out of eval start --no-model)")
    et.add_argument("name")
    et.add_argument("tag", choices=["model", "none"])
    em = add(env, "model", cmd_env_model, "add a model service like the website's quick form: key (secret), base URL and model")
    em.add_argument("--provider", required=True, choices=[p["cli"] for p in MODEL_PROVIDERS])
    em.add_argument("--key", required=True, help="the API key ('-' reads it from standard input; recommended)")
    em.add_argument("--base-url", help="https:// address (default: the provider's; required for custom)")
    em.add_argument("--model", help="model name (default: the provider's, if any)")
    em.add_argument("--prefix", help="variable prefix, e.g. KIMI gives KIMI_API_KEY (default: OPENAI_*, ANTHROPIC_* for Anthropic)")
    em.add_argument("--replace", action="store_true", help="overwrite variables that already exist")
    dom = add(env, "domains", cmd_env_domains, "allowed domains (not used while the platform allows any public address)")
    dsub = dom.add_subparsers(dest="domains_cmd", metavar="ACTION")
    add(dsub, "list", cmd_env_domains, "list domains")
    add(dsub, "set", cmd_env_domains, "replace the list (at most 10)").add_argument("hosts", nargs="+")
    add(dsub, "clear", cmd_env_domains, "remove all domains")
    er = add(env, "route", cmd_env_route, "egress route for evaluations: direct (default), cn (China route) or "
             "overseas (overseas route); without arguments, show it")
    er.add_argument("route", nargs="?", choices=["direct", "cn", "overseas"])
    er.add_argument("--fallback", dest="fallback", action="store_true", default=None,
                    help="when no route node works, connect directly (default)")
    er.add_argument("--no-fallback", dest="fallback", action="store_false",
                    help="when no route node works, refuse the connection instead")

    proj = sub.add_parser("project", help="submit projects and manage versions").add_subparsers(dest="cmd", metavar="ACTION")
    add(proj, "list", cmd_project_list, "your project versions").add_argument("--all", action="store_true", help="include withdrawn versions")
    sr = yes(add(proj, "submit-repo", cmd_project_submit_repo, "submit a public GitHub repository (uses one of 10 daily uploads)"))
    sr.add_argument("url", help="https://github.com/OWNER/REPO, or a branch/folder link .../tree/BRANCH/FOLDER")
    sr.add_argument("--title")
    sr.add_argument("--branch", help="branch, tag or commit (default: the default branch)")
    sr.add_argument("--subdir", help="project folder inside the repository (default: the repository root)")
    up = yes(add(proj, "upload", cmd_project_upload, "upload a complete project ZIP, at most 50 MB (uses one of 10 daily uploads)"))
    up.add_argument("zip")
    up.add_argument("--title")
    sh = add(proj, "show", cmd_project_show, "a version: status, error, public test, execution settings, adapter files")
    sh.add_argument("revision")
    sh.add_argument("--files", action="store_true", help="print the added adapter files in full")
    pw = add(proj, "wait", cmd_project_wait, "wait until preparation finishes (exit 9 if it failed)")
    pw.add_argument("revision")
    pw.add_argument("--timeout", type=int, default=1800)
    pw.add_argument("--interval", type=int, default=15)
    pl = add(proj, "logs", cmd_project_logs, "preparation (build) logs and the public test's agent.log")
    pl.add_argument("revision")
    pl.add_argument("--full", action="store_true")
    yes(add(proj, "confirm", cmd_project_confirm, "confirm a prepared version for evaluation (review it first with show --files)")).add_argument("revision")
    yes(add(proj, "withdraw", cmd_project_withdraw, "withdraw a version that was never evaluated")).add_argument("revision")
    pd = add(proj, "download", cmd_project_download, "download the prepared project of a version")
    pd.add_argument("revision")
    pd.add_argument("-o", "--output")
    pe = add(proj, "evidence", cmd_project_evidence, "design award evidence of a version")
    pe.add_argument("revision")
    pe.add_argument("--notes", help="architecture and reproduction notes ('-' reads standard input)")
    pe.add_argument("--code-url")

    ev = sub.add_parser("eval", help="start and follow evaluations").add_subparsers(dest="cmd", metavar="ACTION")
    st = yes(add(ev, "start", cmd_eval_start, "evaluate a confirmed version once (uses 1 of today's evaluations)"))
    st.add_argument("revision")
    st.add_argument("--phase", help=PHASE_HELP + " (default: the one the website uses)")
    st.add_argument("--no-model", action="store_true", help="this evaluation without a model: no model-related team variables, "
                    "OBSERVER_MODEL_DISABLED=1 (to compare with and without an LLM)")
    sc = yes(add(ev, "selfcheck", cmd_eval_selfcheck, "evaluate 3 times and average (uses 3 of today's evaluations)"))
    sc.add_argument("revision")
    sc.add_argument("--phase", help=PHASE_HELP + " (default: the one the website uses)")
    sc.add_argument("--no-model", action="store_true", help="all 3 evaluations without a model (see eval start --no-model)")
    el = add(ev, "list", cmd_eval_list, "evaluation records (newest first)")
    el.add_argument("--limit", type=int, default=20)
    el.add_argument("--phase", help="only this phase: " + PHASE_HELP)
    es_ = add(ev, "show", cmd_eval_show, "one evaluation: status and score per card ('latest' = newest)")
    es_.add_argument("batch")
    es_.add_argument("--phase", help="'latest' within this phase: " + PHASE_HELP)
    ew = add(ev, "wait", cmd_eval_wait, "wait until an evaluation ends; prints the scores (exit 9 unless scored)")
    ew.add_argument("batch", nargs="?", default="latest")
    ew.add_argument("--phase", help="'latest' within this phase: " + PHASE_HELP)
    ew.add_argument("--timeout", type=int, default=3600)
    ew.add_argument("--interval", type=int, default=20)

    res = sub.add_parser("results", help="scores, agent.log and result downloads").add_subparsers(dest="cmd", metavar="ACTION")
    rs = add(res, "show", cmd_eval_show, "scores per card of an evaluation ('latest' = newest)")
    rs.add_argument("batch")
    rs.add_argument("--phase", help="'latest' within this phase: " + PHASE_HELP)
    rl = add(res, "log", cmd_results_log, "a run's platform diagnostics and agent.log (last part, --tail N, --full, -o FILE)")
    rl.add_argument("run")
    rl.add_argument("--full", action="store_true")
    rl.add_argument("--tail", type=int)
    rl.add_argument("--agent-only", action="store_true", help="only agent.log, without the platform diagnostics")
    rl.add_argument("-o", "--output", help="save the full agent.log to this file")
    rd = add(res, "download", cmd_results_download, "download one card's result ZIP")
    rd.add_argument("run")
    rd.add_argument("-o", "--output")
    ra = add(res, "download-all", cmd_results_download_all, "download all cards of an evaluation as one ZIP")
    ra.add_argument("batch", nargs="?", default="latest")
    ra.add_argument("-o", "--output")
    ra.add_argument("--phase", help="'latest' within this phase: " + PHASE_HELP)

    fin = sub.add_parser("final", help="the version used for the hidden final").add_subparsers(dest="cmd", metavar="ACTION")
    add(fin, "show", cmd_final_show, "show the final version (chosen, or the default best evaluation)")
    add(fin, "set", cmd_final_set, "choose a confirmed version as final").add_argument("revision")
    yes(add(fin, "clear", cmd_final_clear, "clear the choice (the best evaluation's version applies)"))

    add(sub, "quota", cmd_quota, "evaluations and project preparations left today")
    add(sub, "competition", cmd_competition, "current competition mode and phases")
    lb = add(sub, "leaderboard", cmd_leaderboard, "leaderboard (online, practice-projects, practice)")
    lb.add_argument("--phase", help="online | practice-projects | practice (default: the current board)")
    lb.add_argument("--card", help="card slug, e.g. v4-a (default: overall)")
    lb.add_argument("--mine", action="store_true", help="only your team's row")
    lb.add_argument("--limit", type=int, default=500)

    kimi = sub.add_parser("kimi", help="Kimi Coding Plan code").add_subparsers(dest="cmd", metavar="ACTION")
    add(kimi, "status", cmd_kimi_status, "eligibility and your team's code")
    add(kimi, "claim", cmd_kimi_claim, "captain: claim the team's code")
    relay = sub.add_parser("relay", help="temporary Kimi relay for local development (OpenAI-compatible; your API token is the key)")
    add(relay.add_subparsers(dest="cmd", metavar="ACTION"), "status", cmd_relay_status,
        "base URL, model and your team's remaining relay allowance today")
    cr = sub.add_parser("credits", help="sponsor API credit codes").add_subparsers(dest="cmd", metavar="ACTION")
    add(cr, "list", cmd_credits_list, "providers and your team's codes")
    add(cr, "claim", cmd_credits_claim, "claim a code from a provider").add_argument("provider")
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.json = getattr(args, "json", False)
    lang = getattr(args, "lang", None) or language()
    command = " ".join(x for x in (args.group, getattr(args, "cmd", None), getattr(args, "invites_cmd", None),
                                   getattr(args, "avatar_cmd", None), getattr(args, "domains_cmd", None)) if x)
    out = Out(args.json, lang, command)
    if not hasattr(args, "func"):
        if args.json:
            print(json.dumps({"ok": False, "command": command, "error": {"code": "usage", "message": "missing command", "exit_code": EXIT_USAGE}}))
        else:
            parser.parse_args((argv or sys.argv[1:]) + ["--help"])
        return EXIT_USAGE
    token = getattr(args, "token", None) or os.environ.get("SURVEY26_TOKEN") or read_config().get("token")
    api = Api(token.strip() if token else None, getattr(args, "api", None) or os.environ.get("SURVEY26_API") or DEFAULT_API)
    try:
        data = args.func(api, args, out)
        if args.json:
            print(json.dumps({"ok": True, "command": command, "data": data}, ensure_ascii=False, default=str))
        return EXIT_OK
    except CliError as error:
        msg = message_for(error.code, lang, error.detail)
        if error.code == "confirmation_required" and error.detail:
            msg = error.detail + " " + msg
        if args.json:
            print(json.dumps({"ok": False, "command": command, "error": {"code": error.code, "message": msg, "exit_code": error.exit_code}},
                             ensure_ascii=False))
        else:
            print("survey26: " + msg, file=sys.stderr)
        return error.exit_code
    except KeyboardInterrupt:
        return 130


def _entry() -> None:
    sys.exit(main())


if __name__ == "__main__":
    _entry()
