import argparse
import sys
import time
import requests
import sqlite3
import curses
import json
from textwrap import wrap
from dataclasses import dataclass
from html.parser import HTMLParser


TEXT_WIDTH = 90
SCROLL_STEP = 3

DEFAULT_CHAPTER = 989

SAVE_PATH = "/home/pavlo/py/shadowslave/"
DB_PATH = SAVE_PATH + "shadowslave.db"
SOURCE_PATH = SAVE_PATH + "chapters0.json"

argparser = argparse.ArgumentParser(
    prog="Shadow Slave Reader",
    description="Ranobe-style reader for Shadow Slave with auto-fetch and cachingfrom telegra.ph",
)
argparser.add_argument("chapter", type=int, help="Chapter to read", nargs="?")


# links contains a json as an array of tuples [chapter: int, link: str]
with open(SOURCE_PATH, "r", encoding="utf-8") as f:
    links = json.load(f)

MOST_RECENT_CHAPTER = max(chapter for chapter, _ in links)


class ArticleParser(HTMLParser):
    def __init__(self):
        self.article_content = []
        self.is_in_article = False
        self.is_in_title = False
        self.article_title = ""
        super().__init__()

    def handle_starttag(self, tag, attrs):
        if tag == "article":
            self.is_in_article = True
        elif tag == "h1" and self.is_in_article:
            self.is_in_title = True

    def handle_data(self, data):
        if self.is_in_title:
            self.article_title = data
        elif self.is_in_article:
            self.article_content.append(data)

    def handle_endtag(self, tag):
        if tag == "article":
            self.is_in_article = False
        elif tag == "h1" and self.is_in_article:
            self.is_in_title = False


def find_chapter_link(chapter: int) -> str|None:
    for chapter_num, link in links:
        if chapter == chapter_num:
            return link
    return None


class Cache:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        # creates table with article number, title and paragraphs stored as json array of strings
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS chapters (id INT PRIMARY KEY, title TEXT NOT NULL, blocks TEXT NOT NULL)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS current (record_id INT PRIMARY KEY, id INT NOT NULL, line INT NOT NULL)"
        )

    def previous_chapter(self) -> tuple[int, int]:
        cursor = self.conn.execute("SELECT id, line FROM current")
        row = cursor.fetchone()
        if row is None or row[0] is None:
            return 989, 0
        return row[0], row[1]


    def get(self, chapter: int) -> tuple[str, list[str]]|None:
        cursor = self.conn.execute(
            "SELECT title, blocks FROM chapters WHERE id = ?", (chapter,)
        )
        row = cursor.fetchone()
        if row is None:
            return None
        title, blocks_json = row
        blocks = json.loads(blocks_json)
        return title, blocks

    def save_chapter(self, chapter: int, title: str, blocks: list[str]) -> None:
        blocks_json = json.dumps(blocks)
        self.conn.execute(
            "INSERT OR REPLACE INTO chapters (id, title, blocks) VALUES (?, ?, ?)",
            (chapter, title, blocks_json)
        )
        self.conn.commit()

    def save_progress(self, chapter: int, line: int) -> None:
        # current must contain no more than a single entry, which is the last read chapter. By saving progress, we only modify that single entry.
        self.conn.execute(
            "INSERT OR REPLACE INTO current (record_id, id, line) VALUES (1, ?, ?)",
            (chapter, line)
        )
        self.conn.commit()


@dataclass
class Action:
    PREV:   int = 0
    NEXT:   int = 1
    EXIT:   int = 2
    RELOAD: int = 3


class Screen:
    def __init__(self, line: int):
        curses.set_escdelay(25)
        self.scr = curses.initscr()
        self.line = line

    def start(self):
        curses.noecho()
        curses.cbreak()
        curses.curs_set(False)

    @staticmethod
    def _prepare_lines(title: str, blocks: list[str], width: int) -> list[str]:
        lines = [title, "", ""]
        for block in blocks:
            for l in wrap(block, width=width):
                lines.append(l)
            lines.append("")
        return lines

    def scroll(self, title: str, blocks: list[str]) -> Action:
        width = min(curses.COLS, TEXT_WIDTH)
        lines = self._prepare_lines(title, blocks, width)
        pad = curses.newpad(len(lines), width)
        pad.keypad(True)

        for y, l in enumerate(lines):
            attrs = (curses.A_BOLD,) if y == 0 else () # make the title bold
            pad.addstr(y,0, l, *attrs)

        bottom = len(lines) - curses.LINES
        if self.line > bottom:
            self.line = bottom

        while True:
            if self.line > bottom: 
                self.line = bottom
            if self.line < 0: 
                self.line = 0

            try:
                pad.refresh(self.line,0, 0,0, curses.LINES-1,width)

                ch = pad.getch()
                if ch == curses.KEY_UP:      self.line -= SCROLL_STEP
                elif ch == curses.KEY_DOWN:  self.line += SCROLL_STEP
                elif ch == curses.KEY_HOME:  self.line = 0
                elif ch == curses.KEY_END:   self.line = bottom
                elif ch == curses.KEY_NPAGE: self.line += curses.LINES
                elif ch == curses.KEY_PPAGE: self.line -= curses.LINES
                elif ch == curses.KEY_LEFT:  return Action.PREV
                elif ch == curses.KEY_RIGHT: return Action.NEXT
                elif ch == ord('q'):         return Action.EXIT
                elif ch == 27: # ESC or ALT
                    pad.nodelay(True)
                    ch2 = pad.getch()
                    pad.nodelay(False)
                    if ch2 == curses.ERR:
                        # indeed ESC
                        return Action.EXIT
                elif ch == curses.KEY_RESIZE:
                    curses.update_lines_cols()
                    return Action.RELOAD
            except KeyboardInterrupt:
                return Action.EXIT

    def print(self, text: str):
        self.scr.clear()
        self.scr.addstr(0, 0, text)
        self.scr.refresh()

    def stop(self):
        curses.nocbreak()
        curses.curs_set(True)
        curses.echo()
        curses.endwin()


class DisplayError(Exception):
    msg: str


def fetch_chapter(cache: Cache, chapter: int) -> tuple[str, list[str]]:
    link = find_chapter_link(chapter)
    if link is None:
        raise DisplayError(f"chapter {chapter} not found.")

    resp = requests.get(link)
    if not resp.ok:
        raise DisplayError(f"failed to fetch chapter {chapter}: {resp.status_code}")

    parser = ArticleParser()
    parser.feed(resp.text)
    cache.save_chapter(chapter, parser.article_title, parser.article_content)

    return (parser.article_title, parser.article_content)


def display_chapter(screen: Screen, cache: Cache, chapter: int) -> tuple[int, float]:
    while True:
        contents = cache.get(chapter)
        if contents is None:
            screen.print(f"Loading chapter {chapter}...")
            contents = fetch_chapter(cache, chapter)

        title, blocks = contents

        action = Action.RELOAD
        while action == Action.RELOAD:
            action = screen.scroll(title, blocks)

        if action == Action.PREV and chapter > DEFAULT_CHAPTER:
            chapter -= 1
            screen.line = 0
        elif action == Action.NEXT and chapter < MOST_RECENT_CHAPTER:
            chapter += 1
            screen.line = 0

        cache.save_progress(chapter, screen.line)

        if action == Action.EXIT:
            return chapter, estimate_read_percentage(blocks, screen.line)


def estimate_read_percentage(blocks: list[str], line: int) -> float:
    return line / len(Screen._prepare_lines("", blocks, TEXT_WIDTH))


def main():
    cache = Cache(DB_PATH)
    args = argparser.parse_args()
    chapter, line = cache.previous_chapter() if args.chapter is None else (int(args.chapter), 0)

    screen = Screen(line)
    screen.start()

    try:
        chapter, estimated_percentage = display_chapter(screen, cache, chapter)
    except DisplayError as e:
        screen.stop()
        print("Error:", e.msg, end="\r\n")
    except Exception as e:
        screen.stop()
        print("Unexpected error:", e, end="\r\n")
    else:
        screen.stop()
        print(f"Ended at chapter {chapter} ({estimated_percentage:.2%})", end="\r\n")    


if __name__ == "__main__":
    main()
