class ArtelError(Exception):
    exit_code = 1


class UsageError(ArtelError):
    exit_code = 2


class NotAvailable(ArtelError):
    exit_code = 3


class GitError(ArtelError):
    def __init__(self, args_list: list[str], stderr: str):
        self.args_list = list(args_list)
        self.stderr = stderr
        super().__init__(f"git {' '.join(self.args_list)} failed: {stderr.strip()}")
