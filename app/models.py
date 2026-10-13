# -*- coding: utf-8 -*-
"""models.py — p2-4 模型管理器：计划内模型的缓存状态 / 下载 / 删除。

**安全边界（本模块的硬约束）**：对外只认模型**短名**（tiny/base/…），短名经
MODEL_REPOS 映射到固定的仓库 id。客户端**无法**传入 repo / revision / endpoint /
URL —— 服务端绝不按用户输入去请求任意地址，snapshot_download 的 endpoint 参数
始终走 huggingface.co 默认值。新增模型 = 在 MODEL_REPOS 里加一条白名单，不存在
「传个 repo 就下」的旁路。

**缓存布局**：huggingface_hub 的 `models--<org>--<name>/`，
其中 `snapshots/<rev>/` 是模型文件。HF 在支持符号链接的机器上把 snapshots 做成
指向 `blobs/` 的链接，在 Windows（未开开发者模式）则是**真实副本**（本机实测
blobs/ 为空、文件直接落在 snapshots/）——故统计体积必须按解析后的真实路径去重，
否则前者会把同一份文件算两遍。

「已缓存」的判据 = 某个 snapshot 里存在 `model.bin`。只看目录存在会把「只下了
config.json 的半成品」误报成可用，而缺 model.bin 时 WhisperModel 根本起不来。
"""
import logging
from pathlib import Path

log = logging.getLogger("models")

#: 计划内模型短名 → HuggingFace 仓库 id（只增不改；对外接口只用短名）
#: 这些 id 与 faster_whisper.utils._MODELS 中同名条目一致，test_models 有断言钉住
#: （用测试盯库里的映射，运行时仍读本表——既不依赖私有 API，也不会悄悄漂移）。
MODEL_REPOS: dict = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}

#: 计划内模型顺序（与 main.MODEL_CHOICES / UI 下拉一致）
PLANNED_MODELS: tuple = tuple(MODEL_REPOS)

#: 判定「模型可用」的关键文件（缺它 WhisperModel 起不来）
REQUIRED_FILE = "model.bin"

#: 展示用体积单位
_UNITS = ((1024 ** 3, "GB"), (1024 ** 2, "MB"), (1024, "KB"))


def human_size(n: int) -> str:
    """字节 → 人读字符串（GB/MB/KB，一位小数）。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "—"
    if n <= 0:
        return "0 MB"
    for div, unit in _UNITS:
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n} B"


def cache_dir() -> Path:
    """HF 缓存根。**调用时读取**（不是 import 时冻结）——测试可 monkeypatch，
    也便于运行时按 env 变化重取。"""
    from huggingface_hub import constants
    return Path(constants.HF_HUB_CACHE)


def entry_dir(cache: Path, name: str) -> Path:
    """模型短名 → HF 缓存条目目录（models--org--name）。未知短名 → 抛 KeyError。"""
    return Path(cache) / ("models--" + MODEL_REPOS[name].replace("/", "--"))


def dir_size(d: Path) -> int:
    """目录占用字节。按**解析后的真实路径**去重：HF 在支持符号链接的平台上
    snapshots/ 是指向 blobs/ 的链接，按真实路径去重后两种布局都算一次。"""
    total = 0
    seen: set = set()
    try:
        if not Path(d).is_dir():
            return 0
        for f in Path(d).rglob("*"):
            try:
                if not f.is_file():
                    continue
                real = f.resolve()
                if real in seen:
                    continue
                seen.add(real)
                total += f.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def snapshot_revision(entry: Path) -> str | None:
    """取一个已完成的 snapshot 的 revision（无 → None）。"""
    snaps = Path(entry) / "snapshots"
    try:
        if not snaps.is_dir():
            return None
        for s in sorted(snaps.iterdir()):
            if (s / REQUIRED_FILE).is_file():
                return s.name
    except OSError:
        return None
    return None


def is_cached(entry: Path) -> bool:
    """是否已有可用的完整模型（某个 snapshot 含 model.bin）。"""
    return snapshot_revision(entry) is not None


def model_info(cache: Path, name: str, active: str | None = None) -> dict:
    """单个模型的缓存状态（纯读，不联网）。

    size 为整个条目目录占用（含半成品），cached 才是「可用」的判据——两者分开
    报，UI 才能区分「没下过」与「下了一半」。"""
    entry = entry_dir(cache, name)
    exists = entry.is_dir()
    return {
        "name": name,
        "repo": MODEL_REPOS[name],
        "cached": is_cached(entry),
        "present": exists,                     # 目录存在（可能只是半成品）
        "partial": exists and not is_cached(entry),
        "size": dir_size(entry) if exists else 0,
        "revision": snapshot_revision(entry),
        "active": (active == name),
    }


def list_models(cache: Path, active: str | None = None) -> dict:
    """全部计划内模型的状态 + 汇总（缓存根路径一并回给 UI 展示）。"""
    models = [model_info(cache, n, active) for n in PLANNED_MODELS]
    return {
        "models": models,
        "cache_dir": str(cache),
        "total_size": sum(m["size"] for m in models),
        "cached_count": sum(1 for m in models if m["cached"]),
    }


def remove_model(cache: Path, name: str) -> tuple[int, str | None]:
    """删除某模型的缓存条目目录。返回 (释放字节数, 错误)。

    只删 `models--<org>--<name>/` 这一个条目（不动 blobs 根、不动别的仓库）；
    「使用中禁删」由调用方判定（本模块不知道谁在跑）。
    删一半失败 → 报错并返回已释放的部分，不假装成功。"""
    entry = entry_dir(cache, name)
    if not entry.is_dir():
        return 0, f"模型未下载，无需删除: {name}"
    freed = dir_size(entry)
    import shutil
    try:
        shutil.rmtree(entry)
    except OSError as e:
        return freed, f"删除失败: {e}"
    if entry.exists():
        return freed, f"删除后目录仍存在（可能有文件被占用）: {entry}"
    log.info(f"[models] 已删除 {name} 缓存（释放 {human_size(freed)}）")
    return freed, None


# —— 下载（阻塞式；调用方放 to_thread，不占事件循环） ——

def _repo_total_bytes(repo: str) -> int:
    """向 HF 查询仓库文件总字节（用于把进度折算成百分比）。
    失败（离线/限流/无网）返回 0 —— 调用方据此降级为「不确定进度」文案，
    绝不因为查不到大小就拒绝下载（本地已有缓存时下载本就不需要联网）。"""
    try:
        from huggingface_hub import HfApi
        info = HfApi().model_info(repo, files_metadata=True)
        total = sum((s.size or 0) for s in (info.siblings or []))
        return int(total)
    except Exception as e:
        log.debug(f"[models] 查询 {repo} 体积失败（进度降级）: {e}")
        return 0


def safe_emit(on_progress):
    """把进度回调包成**永不抛**的发射器。

    下载是主任务、进度只是展示：回调实现里任何异常（含被替换成会抛的测试替身）
    都不许把下载带崩。模块内所有 on_progress 调用点一律走这里——只在 _Progress
    里 try 是不够的（下载编排自己那几个直接调用同样会漏出去，实测踩过）。"""
    def emit(pct, stage: str) -> None:
        try:
            on_progress(int(pct), str(stage))
        except Exception:
            pass
    return emit


class _Progress:
    """把 HF 每个文件的 tqdm 增量汇成一条总进度（10%–90% 区间）。"""

    def __init__(self, total: int, on_progress, label: str):
        self.total = max(0, int(total or 0))
        self.done = 0
        self.cb = safe_emit(on_progress)
        self.label = label
        self._last = None

    def add(self, n) -> None:
        try:
            self.done += max(0, int(n or 0))
            if self.total > 0:
                pct = 10 + int(80 * min(1.0, self.done / self.total))
            else:
                pct = 30          # 拿不到总体积：给个不确定的中间值（文案里带已下量）
            if pct != self._last:
                self._last = pct
                extra = f"（已下载 {human_size(self.done)}）" if self.total <= 0 else ""
                self.cb(pct, f"下载模型 {self.label}{extra}")
        except Exception:
            pass              # 进度回调永远不许影响下载本身


def download_model(name: str, cache: Path, on_progress, *,
                   local_files_only: bool = False) -> Path:
    """下载某模型到缓存；阻塞调用（务必放 to_thread）。返回 snapshot 目录路径。

    on_progress(pct:int, stage:str) 会被高频调用（每个 tqdm 增量一次），
    实现里必须廉价、且**永不抛**。

    `local_files_only=True`（frozen 离线模式）时不联网：命中缓存则秒回，
    否则抛 —— 由调用方转成用户可见错误。"""
    repo = MODEL_REPOS[name]           # KeyError = 调用方没做白名单校验（不该发生）
    emit = safe_emit(on_progress)
    if local_files_only:
        emit(50, f"离线模式：仅查本地缓存 {name}")
        total = 0
    else:
        emit(3, f"查询模型信息 {repo}")
        total = _repo_total_bytes(repo)
    agg = _Progress(total, on_progress, name)
    emit(10, f"下载模型 {name}（{human_size(total)}）" if total else f"下载模型 {name}")

    from tqdm.auto import tqdm as _tqdm

    class _Tqdm(_tqdm):
        """先把增量记进总进度，再交给 tqdm 自己（disable=True 时 tqdm.update 会
        提前 return、连 self.n 都不加，所以必须在 super() 之前记账）。"""

        def __init__(self, *a, **kw):
            kw["disable"] = True        # 服务端不往控制台刷进度条
            super().__init__(*a, **kw)

        def update(self, n=1):
            agg.add(n)
            return super().update(n)

    from huggingface_hub import snapshot_download
    path = snapshot_download(
        repo_id=repo,
        cache_dir=str(cache),
        tqdm_class=_Tqdm,
        local_files_only=local_files_only,
    )
    emit(95, f"模型 {name} 校验完成")
    return Path(path)
