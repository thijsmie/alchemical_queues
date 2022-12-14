# Welcome to Alchemical Queues

Have you got a small web application with a couple users and a database powered by SQLAlchemy? Do you need to run a couple tasks in the background but does it feel like complete overkill to set up a Celery-based system and have to run a broker like Redis or RabbitMQ just for your three automated emails you send per day? Then you are the target audience of *Alchemical Queues*.

*Alchemical Queues* is a small project that implements safe distributed queues on top of SQLAlchemy. On top of that is an implementation of task queues for which you can run one or more workers. Because it only has one dependency (SQLAlchemy) most likely you are just adding ~300 lines of python to your deployment with no additional external services required.
