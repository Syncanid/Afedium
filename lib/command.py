import inspect
import json
import traceback

from lib.logger import log


class CommandContext:
    def __init__(self, client_id=None):
        self.client_id = client_id
        self.outputs = []

    def reply(self, text: str):
        self.outputs.append(str(text))

    def write(self, text: str):
        self.outputs.append(str(text))

    def get_result(self) -> str:
        return "".join(self.outputs)


class CommandNode:
    def __init__(self, name, description=""):
        self.name = name
        self.description = description
        self.handler = None
        self.subcommands = {}

    def subcommand(self, path: str, handler=None, description=""):
        """
        核心魔法：注册并返回子指令节点对象
        允许 cmd.subcommand("install", handler, "描述")
        """
        parts = str(path).strip().split()
        current = self
        for part in parts:
            if part not in current.subcommands:
                current.subcommands[part] = CommandNode(part)
            current = current.subcommands[part]

        if handler:
            current.handler = handler
        if description:
            current.description = description

        return current  # 返回叶子节点对象，支持链式调用或二次派生

    def get_help(self, prefix="", is_last=True, is_root=False) -> list:
        lines = []
        if not is_root:
            connector = "└── " if is_last else "├── "
            # 如果是纯父节点（没 handler 但有子指令），标上 [指令组]
            desc = f" - {self.description}" if self.description else (" - [指令组]" if self.subcommands else "")
            lines.append(f"{prefix}{connector}{self.name}{desc}")
            child_prefix = prefix + ("    " if is_last else "│   ")
        else:
            child_prefix = prefix

        subs = sorted(self.subcommands.keys())
        for i, sub in enumerate(subs):
            lines.extend(self.subcommands[sub].get_help(child_prefix, i == len(subs) - 1, is_root=False))
        return lines

    def execute(self, ctx, args):
        if args and args[0] in self.subcommands:
            return self.subcommands[args[0]].execute(ctx, args[1:])

        if self.handler:
            try:
                sig = inspect.signature(self.handler)
                if 'output_pipe' in sig.parameters:
                    re_out = self.handler(args=args, output_pipe=ctx)
                else:
                    re_out = self.handler(ctx, args)

                if re_out is not None:
                    if isinstance(re_out, dict):
                        ctx.reply(json.dumps(re_out, ensure_ascii=False, indent=2))
                    elif isinstance(re_out, str):
                        ctx.reply(re_out)
                    elif not isinstance(re_out, bool):
                        ctx.reply(str(re_out))
            except Exception as e:
                err_msg = traceback.format_exc()
                ctx.reply(f"执行异常:\n{err_msg}")
                log.error(f"命令 '{self.name}' 执行异常: {e}")
            return ctx.get_result()
        else:
            help_text = "\n".join(self.get_help())
            ctx.reply(f"这是一个指令组，包含以下子指令:\n{help_text}")
            return ctx.get_result()


root_command = CommandNode("")


def register(command_path: str, handler=None, description="") -> CommandNode:
    """
    注册指令的入口。现在它会返回一个 CommandNode 对象。
    示例: cmd = comm_lib.register("core", desc="核心控制")
    """
    return root_command.subcommand(command_path, handler, description)


def unregister(command_path: str):
    parts = str(command_path).strip().split()
    if not parts: return "路径为空"

    current = root_command
    for part in parts[:-1]:
        if part not in current.subcommands:
            return "命令不存在"
        current = current.subcommands[part]

    last_part = parts[-1]
    if last_part in current.subcommands:
        del current.subcommands[last_part]
        return f"已注销: {' '.join(parts)}"
    return "命令不存在"


def command(command_args: list, client_id=None):
    if not command_args:
        return "错误: 空指令\n"

    cmd_name = command_args[0]
    ctx = CommandContext(client_id=client_id)

    if cmd_name == "help":
        help_text = "\n".join(root_command.get_help(is_root=True))
        return f"--- AFEDIUM 可用指令列表 ---\n{help_text}\n"

    if cmd_name in root_command.subcommands:
        result = root_command.subcommands[cmd_name].execute(ctx, command_args[1:])
        return result + ("\n" if result and not result.endswith('\n') else "")
    else:
        return "未知的指令。输入 'help' 获取可用指令列表。\n"


class DummyDict(dict):
    def keys(self): return root_command.subcommands.keys()


command_list = DummyDict()
