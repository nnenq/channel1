"""Умная обрезка видео: укоротить ролик до нужной длины так, чтобы резы были незаметны.

Использование:
    from reuploader.smartcut import smart_cut, format_report
    report = smart_cut("in.mp4", "out.mp4", target_sec=58)
    print(format_report(report))
"""
from .core import CutError, format_report, smart_cut

__all__ = ["CutError", "format_report", "smart_cut"]
