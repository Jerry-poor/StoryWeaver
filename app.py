from __future__ import annotations

import sys
from types import ModuleType

# Import all modular submodules
import backend.app.config as config
import backend.app.storage as storage
import backend.app.llm as llm
import backend.app.agents as agents
import backend.app.pipeline as pipeline
import backend.app.handlers as handlers

submodules = [config, storage, llm, agents, pipeline, handlers]


class StoryWeaverFacade(ModuleType):
    """Custom module facade that propagates runtime attribute modifications (e.g. mock patches)
    to all submodules sharing the name in their namespace. This guarantees 100% backward
    compatibility with legacy tests using patch.object(app, ...)."""

    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        for mod in submodules:
            if name in mod.__dict__:
                mod.__dict__[name] = value

    def __delattr__(self, name: str) -> None:
        super().__delattr__(name)
        for mod in submodules:
            if name in mod.__dict__:
                del mod.__dict__[name]


# Dynamically replace the class of the current module
sys.modules[__name__].__class__ = StoryWeaverFacade

# Expose everything to the facade namespace
from backend.app.config import *
from backend.app.storage import *
from backend.app.llm import *
from backend.app.agents import *
from backend.app.pipeline import *
from backend.app.handlers import *

if __name__ == "__main__":
    from pathlib import Path
    
    # Allow executing 'python app.py' from root as before
    ROOT = Path(__file__).resolve().parent
    sys.path.insert(0, str(ROOT))
    
    from backend.main import main
    main()
