import sys
import types
from typing import Optional

from app.config import settings

# Ensure compatibility between modern langchain-core and langfuse's langchain callback integration
try:
    import langchain.callbacks.base
except (ImportError, ModuleNotFoundError):
    try:
        import langchain_core.callbacks.base as _cb_base
        import langchain_core.agents as _ag_base
        import langchain_core.documents as _doc_base

        _m_cb = types.ModuleType("langchain.callbacks.base")
        _m_cb.BaseCallbackHandler = _cb_base.BaseCallbackHandler
        sys.modules["langchain.callbacks.base"] = _m_cb

        _m_ag = types.ModuleType("langchain.schema.agent")
        _m_ag.AgentAction = _ag_base.AgentAction
        _m_ag.AgentFinish = _ag_base.AgentFinish
        sys.modules["langchain.schema.agent"] = _m_ag

        _m_doc = types.ModuleType("langchain.schema.document")
        _m_doc.Document = _doc_base.Document
        sys.modules["langchain.schema.document"] = _m_doc
    except Exception:
        pass

try:
    from langfuse.callback import CallbackHandler
except ImportError:
    try:
        from langfuse.langchain import CallbackHandler
    except ImportError:
        CallbackHandler = None


def get_langfuse_handler() -> Optional[CallbackHandler]:
    """
    Checks if settings.langfuse_public_key and settings.langfuse_secret_key are set.
    Returns CallbackHandler(public_key=settings.langfuse_public_key, secret_key=settings.langfuse_secret_key, host=settings.langfuse_host)
    if present, otherwise returns None.
    """
    if not CallbackHandler:
        return None

    if settings.langfuse_public_key and settings.langfuse_secret_key:
        return CallbackHandler(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
    return None
