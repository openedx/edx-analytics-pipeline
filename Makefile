
.PHONY:	requirements test test-requirements .tox upgrade

uninstall:
	pip install -r requirements/pip.txt
	-pip uninstall -y edx.analytics.tasks
	python setup.py clean

install: requirements uninstall
	python setup.py install --force

bootstrap: uninstall
	pip install -r requirements/base.txt --no-cache-dir
	python setup.py install --force

develop: requirements develop-local

develop-local: uninstall
	python setup.py develop
	python setup.py install_data

docker-pull:
	docker pull edxops/analytics_pipeline:latest

docker-shell:
	docker run -v `(pwd)`:/edx/app/analytics_pipeline/analytics_pipeline -it edxops/analytics_pipeline:latest bash

system-requirements:
ifeq (,$(wildcard /usr/bin/yum))
	# This is not great, we can't use these libraries on slave nodes using this method.
	sudo apt-get install -y -q libmysqlclient-dev libpq-dev python-dev libffi-dev libssl-dev libxml2-dev libxslt1-dev
else
	sudo yum install -y -q postgresql-devel libffi-devel
endif

requirements:
	pip install -r requirements/pip.txt
	pip install -r requirements/default.txt --no-cache-dir
	pip install -r requirements/extra.txt --no-cache-dir

test-requirements: requirements
	pip install -r requirements/test.txt --no-cache-dir

reset-virtualenv:
	# without bash, environment variables are not available
	bash -c 'virtualenv --clear ${ANALYTICS_PIPELINE_VENV}/analytics_pipeline'

reset-virtualenv-py3:
	bash -c 'virtualenv --clear --python=$$(which python3) ${ANALYTICS_PIPELINE_VENV}/analytics_pipeline'

COMMON_CONSTRAINTS_TXT=requirements/common_constraints.txt
.PHONY: $(COMMON_CONSTRAINTS_TXT)
$(COMMON_CONSTRAINTS_TXT):
	wget -O "$(@)" https://raw.githubusercontent.com/edx/edx-lint/master/edx_lint/files/common_constraints.txt || touch "$(@)"

upgrade: $(COMMON_CONSTRAINTS_TXT)
	## update the requirements/*.txt files with the latest packages satisfying requirements/*.in
	pip install -qr requirements/pip-tools.txt
	CUSTOM_COMPILE_COMMAND="make upgrade" pip-compile --upgrade -o requirements/pip-tools.txt requirements/pip-tools.in
	CUSTOM_COMPILE_COMMAND="make upgrade" pip-compile --upgrade -o requirements/base.txt requirements/base.in
	CUSTOM_COMPILE_COMMAND="make upgrade" pip-compile --upgrade -o requirements/default.txt requirements/default.in
	CUSTOM_COMPILE_COMMAND="make upgrade" pip-compile --upgrade -o requirements/docs.txt requirements/docs.in
	CUSTOM_COMPILE_COMMAND="make upgrade" pip-compile --upgrade -o requirements/test.txt requirements/test.in

test-docker-local:
	docker run --rm -u root -v `(pwd)`:/edx/app/analytics_pipeline/analytics_pipeline -it edxops/analytics_pipeline:latest make develop-local test-local

test-docker:
	docker run --rm -u root --network="edx-analytics-pipeline_default" -v `(pwd)`:/edx/app/analytics_pipeline/analytics_pipeline -it edxops/analytics_pipeline:latest make reset-virtualenv test-requirements develop-local test-local

test-docker-py3:
	docker run --rm -u root -v `(pwd)`:/edx/app/analytics_pipeline/analytics_pipeline -it edxops/analytics_pipeline:latest make reset-virtualenv-py3 test-requirements develop-local test-local

test-local:
	# TODO: when we have better coverage, modify this to actually fail when coverage is too low.
	rm -rf .coverage
	LUIGI_CONFIG_PATH='config/test.cfg' python -m coverage run --rcfile=./.coveragerc -m nose --with-xunit --xunit-file=unittests.xml -A 'not acceptance'

test: test-requirements develop test-local

test-acceptance: test-requirements
	LUIGI_CONFIG_PATH='config/test.cfg' python -m coverage run --rcfile=./.coveragerc -m nose --nocapture --with-xunit -A acceptance $(ONLY_TESTS)

test-acceptance-py3: test-requirements
	LUIGI_CONFIG_PATH='config/test.cfg' python3 -m coverage run --rcfile=./.coveragerc -m nose --nocapture --with-xunit -A acceptance $(ONLY_TESTS)

test-acceptance-local:
	REMOTE_TASK=$(shell which remote-task) LUIGI_CONFIG_PATH='config/test.cfg' ACCEPTANCE_TEST_CONFIG="/var/tmp/acceptance.json" python -m coverage run --rcfile=./.coveragerc -m nose --nocapture --with-xunit -A acceptance --stop -v $(ONLY_TESTS)

test-acceptance-local-py3:
	REMOTE_TASK=$(shell which remote-task) LUIGI_CONFIG_PATH='config/test.cfg' ACCEPTANCE_TEST_CONFIG="/var/tmp/acceptance.json" python3 -m coverage run --rcfile=./.coveragerc -m nose --nocapture --with-xunit -A acceptance --stop -v $(ONLY_TESTS)

test-acceptance-local-all:
	REMOTE_TASK=$(shell which remote-task) LUIGI_CONFIG_PATH='config/test.cfg' ACCEPTANCE_TEST_CONFIG="/var/tmp/acceptance.json" python -m coverage run --rcfile=./.coveragerc -m nose --nocapture --with-xunit -A acceptance -v

test-acceptance-local-all-py3:
	REMOTE_TASK=$(shell which remote-task) LUIGI_CONFIG_PATH='config/test.cfg' ACCEPTANCE_TEST_CONFIG="/var/tmp/acceptance.json" python3 -m coverage run --rcfile=./.coveragerc -m nose --nocapture --with-xunit -A acceptance -v

quality-local:
	bash -c 'source ${ANALYTICS_PIPELINE_VENV}/analytics_pipeline/bin/activate && isort --check-only --recursive edx/'
	pycodestyle edx

quality-docker-local:
	docker run --rm -u root -v `(pwd)`:/edx/app/analytics_pipeline/analytics_pipeline -it edxops/analytics_pipeline:latest make develop-local quality-local

quality-docker:
	docker run --rm -u root -v `(pwd)`:/edx/app/analytics_pipeline/analytics_pipeline -it edxops/analytics_pipeline:latest make reset-virtualenv test-requirements develop-local quality-local

coverage-docker:
	docker run --rm -u root -v `(pwd)`:/edx/app/analytics_pipeline/analytics_pipeline -it edxops/analytics_pipeline:latest coverage xml

coverage-local: test-local
	python -m coverage html
	python -m coverage xml -o coverage.xml
	diff-cover coverage.xml --html-report diff_cover.html

	isort --check-only --recursive edx/

	# Compute pep8 quality
	diff-quality --violations=pycodestyle --html-report diff_quality_pep8.html
	pycodestyle edx > pep8.report || echo "Not pep8 clean"

	# Compute pylint quality
	pylint -f parseable edx --msg-template "{path}:{line}: [{msg_id}({symbol}), {obj}] {msg}" > pylint.report || echo "Not pylint clean"
	diff-quality --violations=pylint --html-report diff_quality_pylint.html pylint.report

coverage: test coverage-local

docs-requirements:
	pip install -r requirements/docs.txt --no-cache-dir
	python setup.py install --force

docs-local:
	sphinx-build -b html docs/source docs

docs-clean:
	rm -rf docs/*.* docs/_*

docs: docs-requirements docs-local

todo:
	pylint --disable=all --enable=W0511 edx

# for docker devstack
docker-test-acceptance-local:
	LAUNCH_TASK=$(shell which launch-task) REMOTE_TASK=$(shell which remote-task) LUIGI_CONFIG_PATH='config/docker_test.cfg' ACCEPTANCE_TEST_CONFIG="/edx/etc/edx-analytics-pipeline/acceptance.json" python -m coverage run --rcfile=./.coveragerc -m nose --nocapture --with-xunit -A acceptance --stop -v $(ONLY_TESTS)

docker-test-acceptance-local-all:
	LAUNCH_TASK=$(shell which launch-task) REMOTE_TASK=$(shell which remote-task) LUIGI_CONFIG_PATH='config/docker_test.cfg' ACCEPTANCE_TEST_CONFIG="/edx/etc/edx-analytics-pipeline/acceptance.json" python -m coverage run --rcfile=./.coveragerc -m nose --nocapture --with-xunit -A acceptance -v

generate-spark-egg-files:
	# edx-opaque-keys egg
	mkdir -p /var/tmp/edx_egg_files/
	curl -fSL https://files.pythonhosted.org/packages/63/86/d4bf9c7e7a720125b0572c43dda002f72be2cc66313be601cd7b6cccb2ad/edx-opaque-keys-0.4.tar.gz -o /var/tmp/edx-opaque-keys.tar.gz
	cd /var/tmp/ && mkdir -p /var/tmp/edx-opaque-keys/ && tar --strip-components=1 -xzf /var/tmp/edx-opaque-keys.tar.gz -C /var/tmp/edx-opaque-keys/
	cd /var/tmp/edx-opaque-keys && python setup.py bdist_egg
	cp /var/tmp/edx-opaque-keys/dist/edx_opaque_keys-0.4-py2.7.egg /var/tmp/edx_egg_files/edx_opaque_keys.egg

	# edx-ccx-keys agg
	curl -fSL https://files.pythonhosted.org/packages/a4/03/444d30a3859e36ef2273ae6254c011e1fb3a02f4e3dba16008ecf0b4bdb3/edx-ccx-keys-0.2.1.tar.gz -o /var/tmp/edx-ccx-keys.tar.gz
	cd /var/tmp/ && mkdir -p /var/tmp/edx-ccx-keys/  && tar --strip-components=1 -xzf /var/tmp/edx-ccx-keys.tar.gz -C /var/tmp/edx-ccx-keys/
	cd /var/tmp/edx-ccx-keys && python setup.py bdist_egg
	cp /var/tmp/edx-ccx-keys/dist/edx_ccx_keys-0.2.1-py2.7.egg  /var/tmp/edx_egg_files/edx_ccx_keys.egg

test.start_elasticsearch:
	docker-compose up -d
	sleep 20

test.stop_elasticsearch:
	docker-compose stop

test.elasticsearch_integration:
	python -m nose edx/analytics/tasks/tests/elasticsearch_integration

test_integration_with_es: test.start_elasticsearch test.elasticsearch_integration test.stop_elasticsearch
