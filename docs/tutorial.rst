Tutorial
========


To work with *Alchemical Queues* you will need a SQLAlchemy *Engine* which defines the connection to your database. For this tutorial we will use a SQLite database but you can use whatever you already have.

.. code-block:: python

    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///test.db")


We can now initialize the *Alchemical Queue* manager on this engine. This will initialize some SQLAlchemy metadata on the engine.