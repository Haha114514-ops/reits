# -*- coding: utf-8 -*-
"""扫描件 PDF（评估报告常见）OCR 成文本，本地运行。

用法：python ocr_pdf.py <a.pdf> [<b.pdf> ...] [--dpi 150]
输出同名 .txt（覆盖原来抽不出字的 txt），每页前加 "=== p<页码>"，方便回查。
依赖：pip install rapidocr-onnxruntime
"""
import argparse
import os
import sys

import fitz
from rapidocr_onnxruntime import RapidOCR

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+")
    ap.add_argument("--dpi", type=int, default=150)
    a = ap.parse_args()
    eng = RapidOCR()
    for p in a.pdfs:
        doc = fitz.open(p)
        out = []
        for i, page in enumerate(doc):
            txt = page.get_text().strip()
            if len(txt) < 30:  # 只对没文字层的页 OCR
                pix = page.get_pixmap(dpi=a.dpi)
                res, _ = eng(pix.tobytes("png"))
                # 按行（y）再按列（x）排序，尽量还原表格行
                res = sorted(res or [], key=lambda r: (round(r[0][0][1] / 12), r[0][0][0]))
                lines, cur_y, buf = [], None, []
                for box, text, _s in res:
                    y = round(box[0][1] / 12)
                    if cur_y is not None and y != cur_y:
                        lines.append(" ".join(buf)); buf = []
                    cur_y = y
                    buf.append(text)
                if buf:
                    lines.append(" ".join(buf))
                txt = "\n".join(lines)
            out.append(f"=== p{i + 1}\n{txt}")
        open(os.path.splitext(p)[0] + ".txt", "w", encoding="utf-8").write("\n".join(out))
        print(f"{os.path.basename(p)}: {doc.page_count} 页完成")


if __name__ == "__main__":
    main()
