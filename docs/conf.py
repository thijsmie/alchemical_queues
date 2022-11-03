import alchemical_queues

# -- General configuration ------------------------------------------------

extensions = [
    'sphinx.ext.autodoc',
    'sphinx.ext.intersphinx'
]

templates_path = ['_templates']

project = alchemical_queues.__title__
author = alchemical_queues.__author__
copyright = f"2022, {alchemical_queues.__author__} and contributors."
version = alchemical_queues.__version__
release = alchemical_queues.__version__

exclude_patterns = ['_build', 'build']

pygments_style = 'sphinx'

# -- Options for HTML output ----------------------------------------------

html_theme = 'piccolo_theme'

# -- Options for AutoDoc --------------------------------------------------

autodoc_member_order = 'bysource'
