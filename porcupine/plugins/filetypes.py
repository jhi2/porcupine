"""Everything related to filetypes.toml."""
from __future__ import annotations

import argparse
import fnmatch
import logging
import re
import sys
from functools import partial
from pathlib import Path
from typing import Any, Dict, Optional

import tomli
from pygments import lexers
from pygments.util import ClassNotFound

from porcupine import (
    dirs,
    filedialog_kwargs,
    get_parsed_args,
    get_tab_manager,
    menubar,
    settings,
    tabs,
)
from porcupine.settings import global_settings

log = logging.getLogger(__name__)
FileType = Dict[str, Any]
filetypes: dict[str, FileType] = {}


# Sometimes dynamic typing is awesome
def merge_settings(default: object, user: object) -> Any:
    if isinstance(default, dict) and isinstance(user, dict):
        # If a key is in only one of the dicts, include as is.
        # Recurse for keys in both dicts.
        result = {**default, **user}
        for common_key in default.keys() & user.keys():
            result[common_key] = merge_settings(default[common_key], user[common_key])
        return result
    return user


def is_list_of_strings(obj: object) -> bool:
    return isinstance(obj, list) and all(isinstance(item, str) for item in obj)


def load_filetypes() -> None:
    # user_path can't be global var because tests monkeypatch
    user_path = Path(dirs.user_config_dir) / "filetypes.toml"
    defaults_path = Path(__file__).absolute().parent.parent / "default_filetypes.toml"

    with defaults_path.open("rb") as defaults_file:
        default_filetypes = tomli.load(defaults_file)

    user_filetypes: dict[str, FileType] = {}
    try:
        with user_path.open("rb") as user_file:
            user_filetypes = tomli.load(user_file)
    except FileNotFoundError:
        log.info(f"'{user_path}' not found, creating")
        with user_path.open("x") as file:  # error if exists
            file.write(
                """\
# Putting filetype configuration into this file overrides Porcupine's default
# filetype configuration. You can read the default configuration here:
#
#    https://github.com/Akuli/porcupine/blob/master/porcupine/default_filetypes.toml
"""
            )
    except (OSError, UnicodeError, tomli.TOMLDecodeError):
        log.exception(f"reading '{user_path}' failed, using defaults")

    assert not filetypes
    filetypes.update(merge_settings(default_filetypes, user_filetypes))

    for name, filetype in filetypes.items():
        # everything except filename_patterns and shebang_regex is handled by Settings objects
        if "filename_patterns" in filetype and not is_list_of_strings(
            filetype["filename_patterns"]
        ):
            log.error(f"filename_patterns is not a list of strings in [{name}] section")
            del filetype["filename_patterns"]

        if "shebang_regex" in filetype:
            try:
                re.compile(filetype["shebang_regex"])
            except re.error:
                log.error(f"invalid shebang_regex in [{name}] section")
                del filetype["shebang_regex"]

        filetype.setdefault("filename_patterns", [])
        filetype.setdefault("shebang_regex", r"this regex matches nothing^")
        filetype.setdefault("syntax_highlighter", "pygments")

        # Please avoid code like "if this is a C file", in case someone wants to use the same thing for c++
        # Applies to most other filetypes too e.g. Python file .py and Python stub file .pyi
        assert "filetype_name" not in filetype
        filetype["filetype_name"] = name

    filedialog_kwargs["filetypes"] = [
        (
            name,
            [
                # "*.py" doesn't work on windows, but ".py" works and does the same thing
                # See "SPECIFYING EXTENSIONS" in tk_getOpenFile manual page
                pattern.split("/")[-1].lstrip("*")
                for pattern in filetype["filename_patterns"]
            ],
        )
        for name, filetype in filetypes.items()
        if name != "Plain Text"  # can just use "All Files" for this
    ]

    if sys.platform != "darwin":
        # Causes crashes for some Mac users, but not all. See #1092
        filedialog_kwargs["filetypes"].insert(0, ("All Files", ["*"]))


def get_filetype_from_matches(
    matches: dict[str, FileType], they_match_what: str
) -> FileType | None:
    if not matches:
        return None
    if len(matches) >= 2:
        # Last match, because it's more likely from the user's configuration rather than default.
        names = ", ".join(matches.keys())
        log.warning(
            f"{len(matches)} file types match {they_match_what}: {names}. The last match will be"
            " used."
        )
    return list(matches.values())[-1]


def guess_filetype_from_path(filepath: Path) -> FileType | None:
    assert filepath.is_absolute()
    return get_filetype_from_matches(
        {
            name: filetype
            for name, filetype in filetypes.items()
            if any(
                fnmatch.fnmatch(filepath.as_posix(), "*/" + pat)
                for pat in filetype["filename_patterns"]
            )
        },
        str(filepath),
    )


def guess_filetype_from_shebang(content_start: str) -> FileType | None:
    shebang_line = content_start.split("\n")[0]
    matches = {}

    for name, filetype in filetypes.items():
        if re.search(filetype["shebang_regex"], shebang_line) is not None:
            matches[name] = filetype

    return get_filetype_from_matches(matches, f"shebang {shebang_line!r}")


# TODO: take content as argument
def guess_filetype(filepath: Path) -> FileType:
    filetype = guess_filetype_from_path(filepath)
    if filetype is not None:
        return filetype

    try:
        # the shebang is read as utf-8 because the filetype config file
        # is utf-8
        with filepath.open("r", encoding="utf-8") as file:
            # don't read the entire file if it's huge and all on one line
            shebang_line: str | None = file.readline(1000)
    except (UnicodeError, OSError):
        shebang_line = None

    # don't guess from first line of file when it's not a shebang
    if shebang_line is not None and not shebang_line.startswith("#!"):
        shebang_line = None

    if shebang_line is not None:
        filetype = guess_filetype_from_shebang(shebang_line)
        if filetype is not None:
            return filetype

    # if nothing else works, create a new filetype automagically based on pygments
    try:
        lexer = lexers.get_lexer_for_filename(filepath)
    except ClassNotFound:
        if shebang_line is None:
            return filetypes["Plain Text"]  # give up
        lexer = lexers.guess_lexer(shebang_line)
        if isinstance(lexer, lexers.TextLexer):
            return filetypes["Plain Text"]  # give up

    return {
        "pygments_lexer": type(lexer).__module__ + "." + type(lexer).__name__,
        "langserver": None,
    }


def get_filetype_for_tab(tab: tabs.FileTab) -> FileType:
    if tab.path is None:
        return filetypes[global_settings.get("default_filetype", str)]
    # FIXME: this may read the shebang from the file, but the file
    #        might not be saved yet because save_as() sets self.path
    #        before saving, and that's when this runs
    return guess_filetype(tab.path)


def apply_filetype_to_tab(filetype: FileType, tab: tabs.FileTab) -> None:
    log.info(f"applying filetype settings: {filetype!r}")

    with tab.settings.defer_change_events():
        # Reset all options whose values came from the previous filetype.
        # This is needed because previous filetype could have set some option that
        # the new filetype does not set.
        for name in tab.settings.get_options_by_tag("from_filetype"):
            tab.settings.reset(name)

        for name, value in filetype.items():
            # Ignore stuff used only for guessing the correct filetype
            if name not in {"filename_patterns", "shebang_regex"}:
                tab.settings.set(name, value, from_config=True, tag="from_filetype")


def on_path_changed(tab: tabs.FileTab, junk: object = None) -> None:
    log.info(f"file path changed: {tab.path}")
    apply_filetype_to_tab(get_filetype_for_tab(tab), tab)


def after_save(tab: tabs.FileTab, junk: object) -> None:
    if tab.path == Path(dirs.user_config_dir) / "filetypes.toml":
        # Reload all filetypes when the user saves filetypes.toml.
        # If user has chosen a custom filetype, leave the tab alone.
        tabs_with_default_filetype = [
            tab
            for tab in get_tab_manager().tabs()
            if isinstance(tab, tabs.FileTab)
            and tab.settings.get("filetype_name", str) == get_filetype_for_tab(tab)["filetype_name"]
        ]
        filetypes.clear()
        load_filetypes()
        for tab in tabs_with_default_filetype:
            apply_filetype_to_tab(get_filetype_for_tab(tab), tab)


def on_new_filetab(tab: tabs.FileTab) -> None:
    tab.settings.add_option("filetype_name", None, type_=Optional[str])
    on_path_changed(tab)
    tab.bind("<<PathChanged>>", partial(on_path_changed, tab), add=True)
    tab.bind("<<AfterSave>>", partial(after_save, tab), add=True)


def setup_argument_parser(parser: argparse.ArgumentParser) -> None:
    def parse_filetype_name(name: str) -> FileType:
        try:
            return filetypes[name]
        except KeyError:
            raise argparse.ArgumentTypeError(f"no filetype named {name!r}")

    load_filetypes()
    parser.add_argument(
        "-n",
        "--new-file",
        metavar="FILETYPE",
        action="append",
        type=parse_filetype_name,
        help='create a "New File" tab with a filetype from filetypes.toml',
    )


def setup() -> None:
    global_settings.add_option("default_filetype", "Python")

    # load_filetypes() got already called in setup_argument_parser()
    get_tab_manager().add_filetab_callback(on_new_filetab)

    settings.add_combobox(
        "default_filetype",
        "Default filetype for new files:",
        values=sorted(filetypes.keys(), key=str.casefold),
    )

    for name in sorted(filetypes.keys(), key=str.casefold):
        escaped_name = name.replace("/", "//")  # doesn't work in all corner cases
        menubar.add_filetab_command(
            f"Filetypes/{escaped_name}", partial(apply_filetype_to_tab, filetypes[name])
        )

    path = Path(dirs.user_config_dir) / "filetypes.toml"
    menubar.get_menu("Filetypes").add_separator()
    menubar.add_config_file_button(path, menu="Filetypes")
    menubar.add_config_file_button(path)  # goes to "Settings/Config Files"

    new_file_filetypes = get_parsed_args().new_file or []  # argparse can give None
    for filetype in new_file_filetypes:
        tab = tabs.FileTab(get_tab_manager())
        get_tab_manager().add_tab(tab)  # sets default filetype
        apply_filetype_to_tab(filetype, tab)  # sets correct filetype
