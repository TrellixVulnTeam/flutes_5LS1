import contextlib
import io
import os
import sys
from typing import IO, Optional, Dict, Any

from tqdm import tqdm

from .types import PathType

__all__ = [
    "shut_up",
    "progress_open",
    "reverse_open",
]


@contextlib.contextmanager
def shut_up(stderr: bool = True, stdout: bool = False):
    r""" Suppress output (probably generated by external script or badly-written libraries) for ``stderr`` or
    ``stdout``. This method can be used as a decorator, or a context manager:

    .. code:: python

        @shut_up(stderr=True)
        def verbose_func(...):
            ...

        with shut_up(stderr=True):
            ... # verbose stuff

    :param stderr: If ``True``, suppress output from ``stderr``.
    :param stdout: If ``True``, suppress output from ``stdout``.
    """
    # redirect output to /dev/null
    fds = ([1] if stdout else []) + ([2] if stderr else [])
    null_fds = [os.open(os.devnull, os.O_RDWR) for _ in fds]
    output_fds = [os.dup(fd) for fd in fds]
    for null_fd, fd in zip(null_fds, fds):
        os.dup2(null_fd, fd)
    yield
    # restore normal stderr
    for null_fd, output_fd, fd in zip(null_fds, output_fds, fds):
        os.dup2(output_fd, fd)
        os.close(null_fd)


class _ProgressBufferedReader(io.BufferedReader, IO[bytes]):
    def __init__(self, raw: IO[bytes], buffer_size: int = io.DEFAULT_BUFFER_SIZE, *, bar_kwargs: Dict[str, Any]):
        super().__init__(raw, buffer_size)
        file_size = os.fstat(raw.fileno()).st_size
        self.progress_bar = tqdm(total=file_size, **bar_kwargs)

    def __enter__(self):
        self.progress_bar.__enter__()
        return super().__enter__()

    def __exit__(self, exc_type, exc_val, exc_tb):
        if super().__exit__(exc_type, exc_val, exc_tb):
            return True
        return self.progress_bar.__exit__(exc_type, exc_val, exc_tb)

    def close(self) -> None:
        self.progress_bar.close()

    def read(self, size: int = -1) -> bytes:
        ret = super().read(size)
        self.progress_bar.update(len(ret))
        return ret

    def read1(self, size: int = -1) -> bytes:
        ret = super().read1(size)
        self.progress_bar.update(len(ret))
        return ret

    def readinto(self, b: bytearray) -> int:
        ret = super().readinto(b)
        self.progress_bar.update(ret)
        return ret

    def readline(self, size: int = -1) -> bytes:
        ret = super().readline(size)
        self.progress_bar.update(len(ret))
        return ret

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        ret = super().seek(offset, whence)
        self.progress_bar.n = ret
        self.progress_bar.refresh()
        return ret


class progress_open(IO[str]):
    r"""A replacement for ``open`` that shows the progress of reading the file:

    .. code:: python

        with progress_open(path, mode="r") as f:
            # `f` is just what you'd get with `open(path)`, now with a progress bar
            bar = f.progress_bar  # type: tqdm.tqdm

    :param path: Path to the file.
    :param mode: The file open mode. When progress bar is enabled, only read modes ``"r"`` and ``"rb"`` are supported
        (write progress doesn't make a lot of sense). Defaults to ``"r"``.
    :param encoding: Encoding for the file. Only required for ``"r"`` mode. Defaults to ``"utf-8"``.
    :param verbose: If ``False``, the progress bar is not displayed and a normal file object is returned. Defaults to
        ``True``.
    :param buffer_size: The size of the file buffer. Defaults to ``io.DEFAULT_BUFFER_SIZE``.
    :param kwargs: Additional arguments to pass to ``tqdm`` initializer.
    :return: A file object.
    """
    progress_bar: tqdm

    def __new__(cls, path: PathType, mode: str = "r", *, encoding: str = 'utf-8', verbose: bool = True,
                buffer_size: int = io.DEFAULT_BUFFER_SIZE, **kwargs):
        if not verbose:
            return open(path, mode)

        if mode not in ["r", "rb"]:
            raise ValueError(f"Unsupported mode '{mode}'. Only read modes ('r', 'rb') are supported")

        kwargs.setdefault("bar_format", "{l_bar}{bar}| [{elapsed}<{remaining}]")
        buffer = f = _ProgressBufferedReader(io.FileIO(path, mode="r"), buffer_size, bar_kwargs=kwargs)
        if mode == "r":
            f = io.TextIOWrapper(f, encoding=encoding)
            f.progress_bar = buffer.progress_bar
        return f


class _ReverseReadlineFile:
    MAX_CHAR_BYTES = 4  # Maximum length of byte sequences for any character in target encoding

    @staticmethod
    def generator(fp, *, encoding='utf-8', allow_empty_lines=False, buf_size=8192):
        segment = None
        offset = 0

        fp.seek(0, os.SEEK_END)
        file_size = remaining_size = fp.tell()
        while remaining_size > 0:
            cur_buf_size = buf_size
            offset = min(file_size, offset + cur_buf_size)
            fp.seek(file_size - offset)
            buffer_bytes = fp.read(min(remaining_size, cur_buf_size))

            trials = 0
            while True:
                trials += 1
                try:
                    buffer = buffer_bytes.decode(encoding)
                    break
                except UnicodeDecodeError:
                    if trials >= _ReverseReadlineFile.MAX_CHAR_BYTES:
                        raise
                    buffer_bytes = buffer_bytes[1:]
                    cur_buf_size -= 1
                    offset -= 1
            fp.seek(file_size - offset)

            remaining_size -= cur_buf_size
            lines = buffer.split('\n')
            # the first line of the buffer is probably not a complete line so
            # we'll save it and append it to the last line of the next buffer
            # we read
            if segment is not None:
                # if the previous chunk starts right from the beginning of line
                # do not concat the segment to the last line of new chunk
                # instead, yield the segment first
                if buffer[-1] != '\n':
                    lines[-1] += segment
                else:
                    yield segment
            segment = lines[0]
            for index in range(len(lines) - 1, 0, -1):
                if allow_empty_lines or len(lines[index]):
                    yield lines[index]
        # Don't yield None if the file was empty
        if segment is not None:
            yield segment

    def __init__(self, fp: IO, gen):
        self.fp = fp
        self.gen = gen

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.gen) + '\n'

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def readline(self):
        return next(self.gen)

    def close(self):
        self.fp.close()


def reverse_open(path: PathType, *, encoding: str = 'utf-8', allow_empty_lines: bool = False,
                 buffer_size: int = io.DEFAULT_BUFFER_SIZE):
    # Credits: https://stackoverflow.com/questions/2301789/read-a-file-in-reverse-order-using-python
    r"""A generator that returns the lines of a file in reverse order. Usage and syntax is the same as built-in
    method ``open``.

    :param path: Path to file.
    :param encoding: Encoding of file. Defaults to ``"utf-8"``.
    :param allow_empty_lines: If ``False``, empty lines are skipped. Defaults to ``False``.
    :param buffer_size: Buffer size. You probably won't need to change this for most cases. Defaults to
        ``io.DEFAULT_BUFFER_SIZE``.
    """
    if buffer_size < _ReverseReadlineFile.MAX_CHAR_BYTES:
        raise ValueError(f"`buf_size` must be at least {_ReverseReadlineFile.MAX_CHAR_BYTES}")
    fp = open(path, "rb")
    gen = _ReverseReadlineFile.generator(fp, encoding=encoding, allow_empty_lines=allow_empty_lines,
                                         buf_size=buffer_size)
    return _ReverseReadlineFile(fp, gen)
