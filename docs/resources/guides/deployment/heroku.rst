.. _ref_guide_deployment_heroku:

======
Heroku
======

:edb-alt-title: Deploying Gel to Heroku

In this guide we show how to deploy Gel to Heroku using a Heroku PostgreSQL
add-on as the backend.

Because of Heroku's architecture Gel must be deployed with a web app on
Heroku. For this guide we will use a `todo app written in Node <todo-repo_>`_.

.. _todo-repo: https://github.com/geldata/simpletodo/tree/main


Prerequisites
=============

* Heroku account
* ``heroku`` CLI (`install <heroku-cli-install_>`_)
* Node.js (`install <nodejs-install_>`_)

.. _heroku-cli-install: https://devcenter.heroku.com/articles/heroku-cli
.. _nodejs-install:
   https://docs.npmjs.com
   /downloading-and-installing-node-js-and-npm
   #using-a-node-version-manager-to-install-node-js-and-npm


Setup
=====

First copy the code, initialize a new git repo, and create a new heroku app.

.. code-block:: bash

   $ npx degit 'geldata/simpletodo#main' simpletodo-heroku
   $ cd simpletodo-heroku
   $ git init --initial-branch main
   $ heroku apps:create --buildpack heroku/nodejs
   $ gel project init --non-interactive

If you are using the :ref:`JS query builder for Gel <gel-js-qb>` then
you will need to check the ``dbschema/edgeql-js`` directory in to your git
repo after running ``yarn edgeql-js``. The ``edgeql-js`` command cannot be
run during the build step on Heroku because it needs access to a running
|Gel| instance which is not available at build time on Heroku.

.. code-block:: bash

   $ yarn install && npx @gel/generate edgeql-js

The ``dbschema/edgeql-js`` directory was added to the ``.gitignore`` in the
upstream project so we'll remove it here.

.. code-block:: bash

   $ sed -i '/^dbschema\/edgeql-js$/d' .gitignore


Create a PostgreSQL Add-on
==========================

Heroku's smallest PostgreSQL plan, Hobby Dev, limits the number of rows to
10,000, but Gel's standard library uses more than 20,000 rows so we need to
use a different plan. We'll use the `Standard 0 plan <postgres-plans_>`_ for
this guide.

.. _postgres-plans: https://devcenter.heroku.com/articles/heroku-postgres-plans

.. code-block:: bash

   $ heroku addons:create --wait heroku-postgresql:standard-0


Add the Gel Buildpack
=====================

To run Gel on Heroku we'll add the `Gel buildpack <buildpack_>`_.

.. _buildpack: https://github.com/geldata/heroku-buildpack-gel

.. code-block:: bash

   $ heroku buildpacks:add \
       --index 1 \
       https://github.com/geldata/heroku-buildpack-gel.git


Use ``start-gel`` in the Procfile
=================================

To make Gel available to a process prepend the command with ``start-gel``
which is provided by the Gel buildpack. For the sample application in this
guide, the web process is started with the command ``npm start``. If you have
other processes in your application besides/instead of web that need to access
|Gel| those process commands should be prepended with ``start-gel`` too.

.. code-block:: bash

   $ echo "web: start-gel npm start" > Procfile


Deploy the App
==============

Commit the changes and push to Heroku to deploy the app.

.. code-block:: bash

   $ git add .
   $ git commit -m "first commit"
   $ git push heroku main


Scale the web dyno
==================

The default dyno size has 512MB of memory which is a little under powered to
run Gel. Scale the dyno so that it has 1GB of memory available.

.. code-block:: bash

   $ heroku ps:type web=standard-2x

Health Checks
=============

Using an HTTP client, you can perform health checks to monitor the status of
your Gel instance. Learn how to use them with our :ref:`health checks guide
<ref_guide_deployment_health_checks>`.
