from importlib import import_module
from pathlib import Path

from flask import Flask


def create_app(config=None):
    Config = import_module('app.config').Config
    JobManager = import_module('app.services.job_manager').JobManager
    cfg = config if config is not None else Config()

    template_dir = Path(__file__).resolve().parent.parent / 'templates'
    app = Flask(__name__, template_folder=str(template_dir))
    app.config.from_object(cfg)
    setattr(app, 'config_obj', cfg)

    Path(cfg.JOBS_DIR).mkdir(parents=True, exist_ok=True)

    jm = JobManager(cfg)
    setattr(app, 'job_manager', jm)

    bp = import_module(f'{__name__}.routes').bp
    app.register_blueprint(bp)

    return app
