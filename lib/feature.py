import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict

from lib.common import static
from lib.logger import log


FeatureRpcHandler = Callable[[str, Dict[str, Any], Any], Any]
FEATURE_RPC_CONTROL_CODE = 0x30


@dataclass
class FeatureRegistration:
    feature_id: str
    owner: str
    handler: FeatureRpcHandler
    manifest: Dict[str, Any]


_lock = threading.RLock()
_features: Dict[str, FeatureRegistration] = {}


def register_feature(
    feature_id: str,
    owner: str,
    handler: FeatureRpcHandler,
    manifest: Dict[str, Any],
):
    if not feature_id:
        raise ValueError("feature_id 不能为空")
    if not owner:
        raise ValueError("feature 必须声明所属模块")

    with _lock:
        existing = _features.get(feature_id)
        if existing and existing.owner != owner:
            raise ValueError(f"Feature {feature_id} 已被 {existing.owner} 注册")

        normalized_manifest = dict(manifest or {})
        normalized_manifest.setdefault("id", feature_id)
        normalized_manifest.setdefault("provider", owner)
        normalized_manifest.setdefault("transport", "feature_rpc")

        _features[feature_id] = FeatureRegistration(
            feature_id=feature_id,
            owner=owner,
            handler=handler,
            manifest=normalized_manifest,
        )

        if "features" not in static:
            static["features"] = {}
        static["features"][feature_id] = normalized_manifest
        log.info(f"[Feature] {owner} 已注册 {feature_id}")


def unregister_feature(feature_id: str, owner: str):
    with _lock:
        existing = _features.get(feature_id)
        if existing and existing.owner == owner:
            del _features[feature_id]
            if static.get("features", {}).get(feature_id) == existing.manifest:
                static["features"].pop(feature_id, None)
            log.info(f"[Feature] {owner} 已注销 {feature_id}")


def get_feature_manifest(feature_id: str):
    with _lock:
        existing = _features.get(feature_id)
        return existing.manifest if existing else None


def call_feature(feature_id: str, method: str, payload: Dict[str, Any], client_id=None):
    with _lock:
        existing = _features.get(feature_id)
    if not existing:
        return _rpc_error("feature_not_found", f"Feature 不存在: {feature_id}")

    try:
        data = existing.handler(method, payload or {}, client_id)
        return {
            "ok": True,
            "data": data,
            "message": "",
            "error": None,
        }
    except Exception as exc:
        log.error(f"[Feature] {feature_id}.{method} 执行失败: {exc}")
        return _rpc_error("feature_call_failed", str(exc))


def _rpc_error(code: str, message: str):
    return {
        "ok": False,
        "data": None,
        "message": message,
        "error": {
            "code": code,
            "message": message,
            "details": None,
        },
    }
