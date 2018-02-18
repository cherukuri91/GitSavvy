import re
from collections import namedtuple, defaultdict
import unicodedata

import sublime
from sublime_plugin import WindowCommand, TextCommand

from ..commands import GsNavigate
from ..git_command import GitCommand
from ...common import util
from .log import LogMixin
from ..ui_mixins.quick_panel import PanelActionMixin, show_log_panel


BlamedLine = namedtuple("BlamedLine", ("contents", "commit_hash", "orig_lineno", "final_lineno"))

NOT_COMMITED_HASH = "0000000000000000000000000000000000000000"
BLAME_TITLE = "BLAME: {}{}"
COMMIT_HASH_LENGTH = 12


class BlameMixin:
    """
    Some helper functions
    """

    def find_lineno(self):
        pattern = r"^.+ \| +\d+"
        line_start = util.view.get_instance_before_pt(self.view, self.cursor_pt, pattern)
        if line_start is None:
            return 1
        else:
            line = self.view.substr(self.view.find(pattern, line_start))
            _, lineno = line.split("|", 1)
            try:
                return int(lineno.strip().split(" ")[0])
            except Exception:
                return 1

    def find_selected_commit_hash(self):
        hunk_start = util.view.get_instance_before_pt(self.view, self.cursor_pt, r"^\-+ \| \-+")
        if hunk_start is None:
            short_hash_row = 1
        else:
            hunk_start_row, _ = self.view.rowcol(hunk_start)
            short_hash_row = hunk_start_row + 2

        short_hash_pos = self.view.text_point(short_hash_row, 0)
        short_hash = self.view.substr(sublime.Region(short_hash_pos, short_hash_pos + COMMIT_HASH_LENGTH))
        return short_hash.strip()


class GsBlameCommand(PanelActionMixin, WindowCommand, GitCommand):
    @util.view.single_cursor_coords
    def run(self, coords, file_path=None, repo_path=None, commit_hash=None):
        self.coords = coords
        self._file_path = file_path or self.file_path
        self.__repo_path = repo_path or self.repo_path
        self._commit_hash = commit_hash if commit_hash else self.get_commit_hash_for_head()
        sublime.set_timeout_async(self.blame)

    def blame(self):
        original_syntax = self.window.active_view().settings().get('syntax')
        view = self.window.new_file()

        settings = view.settings()
        settings.set("git_savvy.blame_view", True)
        settings.set("git_savvy.repo_path", self.__repo_path)
        settings.set("git_savvy.file_path", self._file_path)

        lineno = self.find_matching_lineno(None, self._commit_hash, self.coords[0] + 1)
        settings.set("git_savvy.lineno", lineno)
        settings.set("git_savvy.commit_hash", self._commit_hash)
        settings.set("git_savvy.blame_view.ignore_whitespace", False)
        settings.set("git_savvy.blame_view.detect_move_or_copy_within", None)
        settings.set("git_savvy.original_syntax", original_syntax)

        view.set_syntax_file("Packages/GitSavvy/syntax/blame.sublime-syntax")
        view.set_scratch(True)
        view.set_read_only(True)

        view.run_command("gs_blame_refresh")
        view.run_command("gs_handle_vintageous")


class GsBlameCurrentFileCommand(LogMixin, TextCommand, GitCommand):

    def run(self, edit, **kwargs):
        self._file_path = self.file_path
        sublime.set_timeout_async(
            lambda: self.run_async(file_path=self._file_path, **kwargs), 0)

    def do_action(self, commit_hash, **kwargs):
        sublime.set_timeout(
            lambda: self.view.window().run_command(
                "gs_blame", {"commit_hash": commit_hash, "file_path": self._file_path}),
            100)

    def log(self, **kwargs):
        savvy_settings = sublime.load_settings("GitSavvy.sublime-settings")
        follow = savvy_settings.get("blame_follow_rename")
        kwargs["follow"] = follow
        return super().log(**kwargs)


class GsBlameRefreshCommand(BlameMixin, TextCommand, GitCommand):
    _highlighted_count = 0  # to be implemented
    _original_color_scheme = None  # to be implemented
    _theme = None  # to be implemented
    _detect_move_or_copy_dict = {
        "file": "-M",
        "commit": "-C",
        "all_commits": "-CCC"
    }

    def run(self, edit):

        settings = self.view.settings()
        commit_hash = settings.get("git_savvy.commit_hash", None)

        self.view.set_name(
            BLAME_TITLE.format(
                self.get_rel_path(self.file_path) if self.file_path else "unknown",
                " at {}".format(commit_hash[0:7]) if commit_hash else ""
            )
        )

        within_what = \
            settings.get("git_savvy.blame_view.detect_move_or_copy_within", None)
        detect_options = self._detect_move_or_copy_dict[within_what] if within_what else None

        content = self.get_content(
            ignore_whitespace=settings.get("git_savvy.ignore_whitespace", False),
            detect_options=detect_options,
            commit_hash=commit_hash
        )

        # only if the content changes
        if content == self.view.substr(sublime.Region(0, self.view.size())):
            return

        was_empty = self.view.size() == 0
        # store viewport for later restoration
        if len(self.view.sel()) > 0:
            old_viewport = self.view.viewport_position()
            cursor_layout = self.view.text_to_layout(self.view.sel()[0].begin())
            yoffset = cursor_layout[1] - old_viewport[1]
        else:
            yoffset = 0

        self.view.run_command("gs_new_content_and_regions", {
            "content": content,
            "regions": {},
            "nuke_cursors": False
        })

        if settings.get("git_savvy.lineno", None) is not None:
            self.select_line(settings.get("git_savvy.lineno"))
            settings.erase("git_savvy.lineno")

        if len(self.view.sel()) > 0:
            if was_empty:
                # if it was opened as a new file
                self.view.show_at_center(self.view.line(self.view.sel()[0].begin()).begin())
            else:
                cursor_layout = self.view.text_to_layout(self.view.sel()[0].begin())
                sublime.set_timeout_async(
                    lambda: self.view.set_viewport_position(
                        (0, cursor_layout[1] - yoffset), animate=False), 100)

    def get_content(self, ignore_whitespace=False, detect_options=None, commit_hash=None):
        savvy_settings = sublime.load_settings("GitSavvy.sublime-settings")

        if commit_hash:
            # git blame does not follow file name changes like git log, therefor we
            # need to look at the log first too see if the file has changed names since
            # selected commit. I would not be surprised if this brakes in some special cases
            # like rebased or multimerged commits
            follow = savvy_settings.get("blame_follow_rename")
            filename_at_commit = self.filename_at_commit(self.file_path, commit_hash, follow=follow)
        else:
            filename_at_commit = self.file_path

        blame_porcelain = self.git(
            "blame", "-p", '-w' if ignore_whitespace else None, detect_options,
            commit_hash, "--", filename_at_commit
        )
        blame_porcelain = unicodedata.normalize('NFC', blame_porcelain)
        blamed_lines, commits = self.parse_blame(blame_porcelain.splitlines())

        commit_infos = {
            commit_hash: self.short_commit_info(commit)
            for commit_hash, commit in commits.items()
        }

        partitions = tuple(self.partition(blamed_lines))

        longest_commit_line = max(
            (line
             for commit_info in commit_infos.values()
             for line in commit_info),
            key=len)

        longest_code_line = max(
            (line.contents for partition in partitions for line in partition),
            key=len
        )

        partitions_with_commits_iter = self.couple_partitions_and_commits(
            partitions=partitions,
            commit_infos=commit_infos,
            left_pad=len(longest_commit_line)
        )

        spacer = (
            "-" * len(longest_commit_line) +
            " | " +
            "-" * (5 + len(longest_code_line)) +
            "\n"
        )

        return spacer.join(partitions_with_commits_iter)

    def parse_blame(self, blame_porcelain):
        lines_iter = iter(blame_porcelain)

        blamed_lines = []
        commits = defaultdict(lambda: defaultdict(str))

        for line in lines_iter:
            commit_hash, orig_lineno, final_lineno, _ = \
                re.match(r"([0-9a-f]{40}) (\d+) (\d+)( \d+)?", line).groups()
            commits[commit_hash]["short_hash"] = commit_hash[:COMMIT_HASH_LENGTH]
            commits[commit_hash]["long_hash"] = commit_hash

            next_line = next(lines_iter)
            while not next_line.startswith("\t"):
                # Iterate through header keys and values.
                try:
                    k, v = re.match(r"([^ ]+) (.+)", next_line).groups()
                except AttributeError as e:
                    # Sometimes git-blame includes keys without values;
                    # since we don't care about these, simply discard.
                    print("Skipping blame line: " + repr(next_line))
                commits[commit_hash][k] = v
                next_line = next(lines_iter)

            # If `next_lines` starts with a tab (and breaks out of the above
            # while loop), it is an actual line of code.  The line following
            # that will be a new header or the end of the file.
            blamed_lines.append(BlamedLine(
                # Strip tab character.
                contents=next_line[1:],
                commit_hash=commit_hash,
                orig_lineno=orig_lineno,
                final_lineno=final_lineno))

        return blamed_lines, commits

    @staticmethod
    def partition(blamed_lines):
        prev_line = None
        current_hunk = []
        for line in blamed_lines:
            if prev_line and line.commit_hash != prev_line.commit_hash:
                yield current_hunk
                current_hunk = []

            prev_line = line
            current_hunk.append(line)
        yield current_hunk

    @staticmethod
    def short_commit_info(commit):
        if commit["long_hash"] == NOT_COMMITED_HASH:
            return ("Not committed yet.", )

        summary = commit["summary"]
        if len(summary) > 40:
            summary = summary[:36] + " ..."
        author_info = commit["author"] + " " + commit["author-mail"]
        time_stamp = util.dates.fuzzy(commit["author-time"]) if commit["author-time"] else ""

        return (summary, commit["short_hash"], author_info, time_stamp)

    @staticmethod
    def couple_partitions_and_commits(partitions, commit_infos, left_pad):
        left_fallback = " " * left_pad
        right_fallback = ""

        for partition in partitions:
            output = ""
            commit_info = commit_infos[partition[0].commit_hash]
            left_len = len(commit_info)
            right_len = len(partition)
            total_lines = len(max((commit_info, partition), key=len))

            for i in range(total_lines):
                left = commit_info[i] if i < left_len else left_fallback
                right = partition[i].contents if i < right_len else right_fallback
                lineno = partition[i].final_lineno if i < right_len else right_fallback

                output += "{left: <{left_pad}} | {lineno: >4} {right}\n".format(
                    left=left,
                    left_pad=left_pad,
                    lineno=lineno,
                    right=right)
                output = output.strip() + "\n"

            yield output

    def select_line(self, lineno):
        pattern = r".{{30}} \| {lineno: >4}\s".format(lineno=lineno)
        corresponding_region = self.view.find(pattern, 0)
        blame_view_pt = corresponding_region.end()
        if blame_view_pt >= 0:
            self.view.sel().clear()
            self.view.sel().add(sublime.Region(blame_view_pt, blame_view_pt))


class GsBlameActionCommand(BlameMixin, PanelActionMixin, TextCommand, GitCommand):
    selected_index = 0
    """
    Be careful when changing the order since some commands depend on the
    the index. Goto Default.sublime-keymap under section BLAME VIEW to see
    more details on it which.
    """
    default_actions = [
        ["show_commit", "Show Commit"],
        ["blame_neighbor", "Blame a commit before this line's commit", (),
            {'position': "older", 'selected': True}],
        ["blame_neighbor", "Blame previous commit", (), {'position': "older"}],
        ["blame_neighbor", "Blame next commit", (), {'position': "newer"}],
        ["pick_new_commit", "Pick another commit to blame"],
        ["show_file_at_commit", "Show file at current commit"],
        ["show_file_at_commit", "Show file at selected commit", (), {"from_line": True}],
    ]

    @util.view.single_cursor_pt
    def run(self, cursor_pt, edit, pre_selected_index=None):
        self.cursor_pt = cursor_pt
        super().run(pre_selected_index=pre_selected_index)

    def update_actions(self):
        # a deepcopy
        self.actions = [act.copy() for act in self.default_actions]
        selected_commit = self.find_selected_commit_hash()
        if selected_commit:
            for act in self.actions:
                act[1] = act[1].replace("selected commit", selected_commit[0:7])

    def show_commit(self):
        # Uncommitted blocks.
        commit_hash = self.find_selected_commit_hash()
        if not commit_hash:
            return

        self.view.window().run_command("gs_show_commit", {"commit_hash": commit_hash})

    def newst_commit_for_file(self):
        savvy_settings = sublime.load_settings("GitSavvy.sublime-settings")
        follow = savvy_settings.get("blame_follow_rename")
        return self.git(
            "log", "--format=%H",
            "--follow" if follow else None, "-n", "1", self.file_path).strip()

    def blame_neighbor(self, position, selected=False):
        savvy_settings = sublime.load_settings("GitSavvy.sublime-settings")
        follow = savvy_settings.get("blame_follow_rename")

        if position == "newer" and selected:
            raise Exception("blame a commit after selected commit is confusing")

        settings = self.view.settings()
        if selected:
            commit_hash = self.find_selected_commit_hash().strip()
        else:
            commit_hash = settings.get("git_savvy.commit_hash")

        neighbor_hash = self.neighbor_commit(commit_hash, position, follow=follow)
        if neighbor_hash:
            settings.set("git_savvy.commit_hash", neighbor_hash)

        # if there is a change, refresh blame interface
        if commit_hash == settings.get("git_savvy.commit_hash"):
            return

        # set line number
        lineno = self.find_matching_lineno(
            commit_hash, settings.get("git_savvy.commit_hash"), self.find_lineno())

        settings.set("git_savvy.lineno", lineno)
        self.view.run_command("gs_blame_refresh")

    def show_file_at_commit(self, from_line=False):
        settings = self.view.settings()

        if from_line:
            commit_hash = self.find_selected_commit_hash() or 'HEAD'
        else:
            commit_hash = settings.get("git_savvy.commit_hash", "HEAD")

        lineno = self.find_lineno()
        if from_line:
            lineno = self.find_matching_lineno(
                settings.get("git_savvy.commit_hash"), commit_hash, lineno)

        self.view.window().run_command("gs_show_file_at_commit", {
            "commit_hash": commit_hash,
            "filepath": self.file_path,
            "lineno": lineno,
            "lang": settings.get('git_savvy.original_syntax', None)
        })

    def pick_new_commit(self):
        self.view.run_command("gs_blame_pick_commit", {
            "commit_hash": self.view.settings().get("git_savvy.commit_hash"),
        })


class GsBlamePickCommitCommand(TextCommand, GitCommand):

    def run(self, edit, commit_hash=None):
        sublime.set_timeout_async(lambda: self.run_async(self.file_path), 0)

    def run_async(self, file_path):
        self.commit_hash = self.view.settings().get("git_savvy.commit_hash")
        show_log_panel(
            self.log_generator(file_path=file_path, follow=True),
            self.on_done,
            selected_index=lambda entry: entry == self.commit_hash,
            on_highlight=self.on_done
        )

    def on_done(self, commit_hash):
        # Canceled panel
        if commit_hash is None:
            commit_hash = self.commit_hash

        self.view.settings().set("git_savvy.commit_hash", commit_hash)
        self.view.run_command("gs_blame_refresh")


class GsBlameToggleSetting(BlameMixin, TextCommand):

    """
    Toggle view settings: `ignore_whitespace`, `detect_move_or_copy_within_file`,
    `detect_move_or_copy_within_commit` and `detect_move_or_copy_within_all_commits`.
    """
    @util.view.single_cursor_pt
    def run(self, cursor_pt, edit, setting, value=None):
        self.cursor_pt = cursor_pt
        setting_str = "git_savvy.blame_view.{}".format(setting)
        settings = self.view.settings()
        if setting == "detect_move_or_copy_within":
            detect_move_or_copy_within = settings.get(setting_str)
            if detect_move_or_copy_within == value:
                detect_move_or_copy_within = None
            else:
                detect_move_or_copy_within = value

            settings.set(setting_str, detect_move_or_copy_within)
        else:
            settings.set(setting_str, not settings.get(setting_str))

        self.view.window().status_message("{} is now {}".format(setting, settings.get(setting_str)))
        self.view.settings().set("git_savvy.lineno", self.find_lineno())
        self.view.run_command("gs_blame_refresh")


class GsBlameNavigateChunkCommand(GsNavigate):

    """
    Move cursor to the next (or previous) different commit
    """

    offset = 0

    def get_available_regions(self):
        return [
            branch_region
            for region in self.view.find_by_selector(
                "constant.numeric.commit-hash.git-savvy"
            )
            for branch_region in self.view.lines(region)]
