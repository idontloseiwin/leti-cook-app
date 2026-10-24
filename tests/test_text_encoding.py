"""No committed text is double-encoded UTF-8 (UTF-8 read as Windows-1252 and saved again).

A merge or editor that guesses the wrong encoding turns an ellipsis into three
Latin-1 characters; the shipped bundle then shows them. Every UTF-8 text file in the repository, including the built
frontend, is checked. No build, server or database.
"""
from pathlib import Path
import re
import unittest
ROOT=Path(__file__).resolve().parents[1]
SKIP_DIRS={'.git','node_modules','__pycache__','build','.claude'}
BINARY_SUFFIXES={'.woff','.woff2','.png','.jpg','.jpeg','.gif','.ico','.webp','.pdf','.zip','.gz'}
# A UTF-8 lead byte shown as Windows-1252, followed by continuation bytes shown the same way.
CANDIDATE=re.compile('[Â-ô][\u0080-ÿŒœŠšŸŽžƒˆ˜'
                     '–—‘-„†-•…‰‹›€™]{1,3}')

def double_encoded(text):
    for match in CANDIDATE.finditer(text):
        try:
            restored=match.group(0).encode('cp1252').decode('utf-8')
        except (UnicodeEncodeError,UnicodeDecodeError):
            continue
        if len(restored)==1 and restored!=match.group(0):
            yield match.group(0),restored

def repository_text_files():
    for path in ROOT.rglob('*'):
        if not path.is_file() or SKIP_DIRS & set(path.relative_to(ROOT).parts) or path.suffix.lower() in BINARY_SUFFIXES:
            continue
        try:
            yield path,path.read_text(encoding='utf-8')
        except (UnicodeDecodeError,OSError):
            continue

class TextEncodingTests(unittest.TestCase):
    maxDiff=None

    def test_detector_finds_the_known_forms(self):
        broken='Loading… · — ×'.encode('utf-8').decode('cp1252')
        self.assertEqual({restored for _,restored in double_encoded(broken)},{'…','·','—','×'})
        self.assertEqual(list(double_encoded('Loading… café · — Obéd')),[])

    def test_repository_text_is_not_double_encoded(self):
        found=[]
        checked=0
        for path,text in repository_text_files():
            checked+=1
            for number,line in enumerate(text.splitlines(),1):
                for broken,restored in double_encoded(line):
                    found.append(f'{path.relative_to(ROOT).as_posix()}:{number}: {broken!a} should be {restored!a}')
        self.assertGreater(checked,300)
        self.assertEqual(found[:20],[])

if __name__=='__main__':unittest.main()
