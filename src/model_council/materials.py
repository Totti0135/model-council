"""What the council is given to read.

A question is usually about something — a spec, a log, a screenshot, a diff —
and until now the only way to put that thing in front of the members was to
paste it into `prompt`. That is expensive in the one place nobody watches: the
prompt is an argument the caller *writes*, so a long document costs a full copy
of itself in generated tokens on every call, and what the members receive is
whatever the caller managed to reproduce — which, for a long document, is not
reliably the document. A council reviewing a paraphrase is not reviewing the
thing, and nothing in the transcript would say so.

`materials` is the other way in. Name the thing once; this server reads it, and
every member is handed the same bytes in the same place: ahead of the question,
identically for each member and each round. The position is not decoration.
Identical bytes are what make the answers comparable — nobody is arguing with a
different copy — and an identical prefix is the thing an endpoint's cache can
match across a discussion that asks several questions about one document.

Images arrive this way too, which no amount of pasting could do. That matters
more than it looks: a caller describing a screenshot in prose hands every member
the *same* description, so a detail the caller missed is missed by the whole
council at once, and cross-review cannot recover it. An image is the one kind of
material where passing it badly quietly removes the independence the council is
for.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

# Caps, so a mistyped path fails here — locally, in a sentence naming the file —
# instead of a minute later as a 413 from every member at once, or as this
# process reading a log file until it dies.
MAX_TEXT_BYTES = 2_000_000
MAX_IMAGE_BYTES = 5_000_000
MAX_TOTAL_BYTES = 16_000_000
MAX_MATERIALS = 24

# What both wire formats can carry as an image.
_BY_EXTENSION = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# The extension is a claim; these are the bytes. A .jpg that is really a PNG is
# common enough (anything that has been through a screenshot tool and a rename)
# and the endpoints reject the mismatch, so believe the file over its name.
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def _size(n: int) -> str:
    """A size a reader can act on. Rounding a 900-byte file to '0 KB' reads as an
    empty one, which is a thing worth checking and a thing that never happened."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


class MaterialError(Exception):
    """The material could not be loaded, and the call must not proceed without it.

    Unlike a member that fails — which degrades the council to the members that
    answered — this stops the call. A council asked to review a document it was
    not given would answer anyway, fluently, about nothing.
    """


class Material(BaseModel):
    """One thing the council is given to read."""

    label: str = Field(
        default="",
        description="What this is, as the members should see it: 'PRD v3', "
                    "'the failing test', 'checkout mockup'. Defaults to the "
                    "file's name.")
    path: str = Field(
        default="",
        description="Path to a file on this server's machine. Prefer this over "
                    "pasting the contents into `prompt`: the members are handed "
                    "the file's exact bytes, and you do not spend a copy of the "
                    "whole document writing this call. Images (.png .jpg .gif "
                    ".webp) are sent as images to members that can see them. "
                    "A server running over HTTP accepts paths only if its "
                    "operator allowed a directory.")
    text: str = Field(
        default="",
        description="The material inline, for something with no file behind it "
                    "— output you generated, a fragment you already hold. When "
                    "the thing is on disk, give `path` instead.")


@dataclass(frozen=True)
class Loaded:
    """A material that has been read and is ready to put in a request."""

    label: str
    media_type: str = ""      # set for images, empty for text
    text: str = ""            # set for text
    data: str = ""            # base64, set for images
    size: int = 0             # bytes on the wire before encoding
    origin: str = ""          # the path it came from, when it came from one

    @property
    def is_image(self) -> bool:
        return bool(self.media_type)

    def describe(self) -> str:
        what = (f"{self.media_type}, {_size(self.size)}" if self.is_image
                else f"{_size(self.size)} of text")
        where = f" from {self.origin}" if self.origin else ""
        return f"{self.label} ({what}{where})"


@dataclass(frozen=True)
class Policy:
    """Whether this server may read a caller's paths, and from where.

    Over stdio the answer is yes and needs no flag, on the same reasoning that
    lets the loopback bind skip `--allow`: the caller launched this process, so
    it already reads every file this process can. Over HTTP the reasoning is
    gone. A path from an unauthenticated caller would make a shared council into
    a file-read primitive on its host, and one that forwards what it reads to an
    external provider. So HTTP refuses paths outright unless the operator names
    a directory to serve them from.
    """

    allow_paths: bool = True
    root: Path | None = None
    why: str = ""             # what to tell a caller whose path was refused

    def __post_init__(self) -> None:
        # The root is compared against resolved paths, so it has to be resolved
        # itself or the comparison is between two spellings of the same
        # directory. On macOS /var is a symlink to /private/var, which makes an
        # unresolved root reject every file inside it — and the refusal names
        # two paths that look identical.
        if self.root is not None:
            object.__setattr__(self, "root", Path(self.root).expanduser().resolve())


_POLICY = Policy()


def set_policy(policy: Policy) -> None:
    global _POLICY
    _POLICY = policy


def get_policy() -> Policy:
    return _POLICY


def _resolve(raw: str) -> Path:
    path = Path(raw).expanduser()
    policy = _POLICY
    if not policy.allow_paths:
        raise MaterialError(
            f"this server will not read files from its own host on request"
            f"{': ' + policy.why if policy.why else ''}. Pass the contents as "
            f"`text` instead of `path`.")
    try:
        resolved = path.resolve()
    except OSError as e:
        raise MaterialError(f"could not resolve {raw!r}: {e}") from None
    # After resolve(), so a symlink pointing out of the root is caught by where
    # it lands rather than by where it sits.
    if policy.root is not None and not resolved.is_relative_to(policy.root):
        raise MaterialError(
            f"{resolved} is outside {policy.root}, the only directory this "
            f"server is allowed to read material from")
    if not resolved.exists():
        extra = "" if Path(raw).is_absolute() else f" (relative paths resolve against {Path.cwd()})"
        raise MaterialError(f"no such file: {resolved}{extra}")
    if not resolved.is_file():
        raise MaterialError(f"{resolved} is not a file")
    return resolved


def _sniff(blob: bytes, suffix: str) -> str:
    for magic, media_type in _MAGIC:
        if blob.startswith(magic):
            return media_type
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    # No signature. Only trust the extension if it claims an image format we
    # would have recognised — a .png whose bytes are not a PNG is a broken file,
    # and sending it is a 400 from every member in parallel.
    if suffix in _BY_EXTENSION:
        raise MaterialError(
            f"the file's extension says {_BY_EXTENSION[suffix]} but its contents "
            f"are not a {suffix.lstrip('.')} image")
    return ""


def _from_path(label: str, raw: str) -> Loaded:
    resolved = _resolve(raw)
    try:
        blob = resolved.read_bytes()
    except OSError as e:
        raise MaterialError(f"could not read {resolved}: {e}") from None
    if not blob:
        raise MaterialError(f"{resolved} is empty")

    label = label or resolved.name
    media_type = _sniff(blob, resolved.suffix.lower())
    if media_type:
        if len(blob) > MAX_IMAGE_BYTES:
            raise MaterialError(
                f"{resolved} is {len(blob) / 1_000_000:.1f} MB; images are capped "
                f"at {MAX_IMAGE_BYTES // 1_000_000} MB")
        return Loaded(label=label, media_type=media_type,
                      data=base64.b64encode(blob).decode("ascii"),
                      size=len(blob), origin=str(resolved))

    if len(blob) > MAX_TEXT_BYTES:
        raise MaterialError(
            f"{resolved} is {len(blob) / 1_000_000:.1f} MB; text material is capped "
            f"at {MAX_TEXT_BYTES // 1_000_000} MB. Send the part the council needs.")
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        raise MaterialError(
            f"{resolved} is neither UTF-8 text nor an image format the members "
            f"can be shown (png, jpeg, gif, webp)") from None
    return Loaded(label=label, text=text, size=len(blob), origin=str(resolved))


def load(specs: list[Material] | None) -> list[Loaded]:
    """Read every material, or raise with the reason and the file that caused it."""
    specs = [s for s in (specs or []) if (s.path or "").strip() or (s.text or "").strip()]
    if not specs:
        return []
    if len(specs) > MAX_MATERIALS:
        raise MaterialError(f"{len(specs)} materials; at most {MAX_MATERIALS} per call")

    out: list[Loaded] = []
    total = 0
    for i, spec in enumerate(specs, 1):
        label = (spec.label or "").strip()
        path = (spec.path or "").strip()
        text = spec.text or ""
        # Both set is not a preference to resolve quietly: one of them is what
        # the caller meant and the other is stale, and picking wrong sends the
        # council a document nobody asked about.
        if path and text.strip():
            raise MaterialError(
                f"material {label or i} sets both `path` and `text`; give one. "
                f"`path` for a file, `text` for something with no file behind it.")
        try:
            loaded = (_from_path(label, path) if path
                      else Loaded(label=label or f"Material {i}", text=text,
                                  size=len(text.encode("utf-8"))))
        except MaterialError as e:
            raise MaterialError(f"material {label or i}: {e}") from None
        total += loaded.size
        if total > MAX_TOTAL_BYTES:
            raise MaterialError(
                f"the materials add up to more than {MAX_TOTAL_BYTES // 1_000_000} MB")
        out.append(loaded)
    return out


def heading(m: Loaded) -> str:
    """The line that introduces a material to the members.

    Labelled and delimited, because several of them arrive in a row and a model
    that cannot tell where the spec ends and the log begins will answer about
    the seam.
    """
    return f"--- MATERIAL: {m.label}{' (image)' if m.is_image else ''} ---"


def render_for_seat(docs: list[Loaded]) -> str:
    """The materials as text, for a seat this server cannot call.

    `revision_prompt` writes the prompt for a subagent the caller runs, and that
    prompt has to account for the material the members were shown — a seat that
    revises without it is arguing about a document it never saw.

    A material read from a file is named rather than inlined. Inlining it would
    hand the caller back the whole document as something it must then write out
    again to spawn the subagent, which is the cost `materials` exists to remove;
    the caller has the path and can open it. Material with no file behind it has
    nowhere to point, so it goes in whole.
    """
    if not docs:
        return ""
    blocks = []
    for m in docs:
        if m.origin:
            what = "image" if m.is_image else "full contents"
            blocks.append(f"{heading(m)}\n[the members were shown the {what} of "
                          f"{m.origin} here. Give this seat the same file, in this "
                          f"position, ahead of everything below.]")
        else:
            blocks.append(f"{heading(m)}\n{m.text}")
    return "\n\n".join(blocks)
