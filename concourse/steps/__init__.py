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

import os

import mako.template
import mako.lookup

import makoutil

steps_dir = os.path.abspath(os.path.dirname(__file__))
template_lookup = mako.lookup.TemplateLookup(directories=(steps_dir,))


def step_template(name):
    with makoutil.template_lock:
        return template_lookup.get_template(f'/{name}.mako')


def step_def(name):
    template = step_template(name)

    return template.get_def(name + '_step').render


def step_lib_def(name):
    template = step_template(name)

    return template.get_def(name + '_step_lib').render


def step_lib(name):
    module_file = os.path.join(steps_dir, name + '.py')
    with open(module_file) as f:
        return f.read()
