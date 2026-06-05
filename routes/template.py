"""
CASI — Template download route.

GET /api/template/download
  Returns the blank CASI Excel template for users to fill in.
  No authentication required — anyone with the link can grab it.
"""

import os
from flask import Blueprint, send_from_directory, current_app

bp = Blueprint('template', __name__, url_prefix='/api/template')

_STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static')
_TEMPLATE   = 'CASI_Template.xlsx'


@bp.route('/download')
def download_template():
    return send_from_directory(
        _STATIC_DIR,
        _TEMPLATE,
        as_attachment=True,
        download_name='CASI_Template.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
