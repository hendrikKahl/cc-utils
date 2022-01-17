# Copyright (c) 2019-2020 SAP SE or an SAP affiliate company. All rights reserved. This file is
# licensed under the Apache Software License, v. 2 except as noted otherwise in the LICENSE file
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import pytest

import model.elasticsearch as examinee
from model.base import ModelValidationError


@pytest.fixture
def required_dict():
    return {
        'endpoint_url': 'foo',
        'endpoints': 'foo',
    }


def test_validation_fails_on_missing_required_key(required_dict):
    for key in required_dict.keys():
        test_dict = required_dict.copy()
        test_dict.pop(key)
        element = examinee.ElasticSearchConfig(
            name='foo',
            raw_dict=test_dict,
            type_name='elasticsearch',
        )
        with pytest.raises(ModelValidationError):
            element.validate()


def test_validation_succeeds_on_required_dict(required_dict):
    element = examinee.ElasticSearchConfig(
        name='foo',
        raw_dict=required_dict,
        type_name='elasticsearch',
    )
    element.validate()


def test_validation_fails_on_unknown_key(required_dict):
    # since optional attributes are defined for ElasticSearchConfig, test should fail
    test_dict = {**required_dict, **{'foo': 'bar'}}
    element = examinee.ElasticSearchConfig(
        name='foo',
        raw_dict=test_dict,
        type_name='elasticsearch',
    )
    with pytest.raises(ModelValidationError):
        element.validate()
