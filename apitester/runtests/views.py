# -*- coding: utf-8 -*-
"""
Views of runtests app
"""

import json
import urllib

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.urls import reverse_lazy, reverse
from django.views.generic import TemplateView
from django.views.generic.edit import CreateView, UpdateView, DeleteView

from obp.api import API, APIError

from .forms import TestConfigurationForm
from .models import TestConfiguration


# TODO: These have to map to attributes of models.TestConfiguration
URLPATH_REPLACABLES = [
    'API_VERSION',
    'USERNAME', 'USER_ID', 'PROVIDER_ID',
    'BANK_ID', 'BRANCH_ID', 'ATM_ID', 'PRODUCT_CODE',
    'ACCOUNT_ID', 'VIEW_ID', 'TRANSACTION_ID', 'COUNTERPARTY_ID',
    'OTHER_ACCOUNT_ID',
    'CUSTOMER_ID', 'MEETING_ID', 'CONSUMER_ID',
    'FROM_CURRENCY_CODE', 'TO_CURRENCY_CODE',
]


class IndexView(LoginRequiredMixin, TemplateView):
    """Index view for runtests"""
    template_name = "runtests/index.html"

    def get_testconfigs(self, testconfig_pk):
        testconfigs = {
            'available': [],
            'selected': None,
        }
        testconfigs['available'] = TestConfiguration.objects.filter(
            owner=self.request.user).order_by('name')
        if testconfig_pk:
            try:
                testconfigs['selected'] = TestConfiguration.objects.get(
                    owner=self.request.user,
                    pk=testconfig_pk,
                )
            except TestConfiguration.DoesNotExist as err:
                raise PermissionDenied
        return testconfigs

    def get_context_data(self, **kwargs):
        context = super(IndexView, self).get_context_data(**kwargs)
        calls = []
        testconfig_pk = kwargs.get('testconfig_pk', 0)
        testconfigs = self.get_testconfigs(testconfig_pk)
        api = API(self.request.session.get('obp'))
        if 'selected' in testconfigs and testconfigs['selected']:
            api_version = testconfigs['selected'].api_version
            try:
                swagger = api.get_swagger(api_version)
            except APIError as err:
                messages.error(self.request, err)
            else:
                for path, data in swagger['paths'].items():
                    # Only GET requests for now
                    if 'get' in data:
                        call = {
                            'urlpath': path,
                            'method': 'get',
                            'summary': data['get']['summary'],
                            'responseCode': 200,
                        }
                        calls.append(call)
                calls = sorted(calls, key=lambda call: call['summary'])
        context.update({
            'calls': calls,
            'testconfigs': testconfigs,
            'testconfig_pk': testconfig_pk,
        })
        return context


class RunView(LoginRequiredMixin, TemplateView):
    """Run an actual test against the API"""
    template_name = "runtests/index.html"

    def render_to_response(self, context, **response_kwargs):
        return JsonResponse(self.get_data(context), **response_kwargs)

    def get_data(self, context):
        # This should ensure everything in context is JSON-serialisable
        if 'view' in context:
            del context['view']
        return context

    def api_replace(self, string, match, value):
        """Helper to replace format strings from the API"""
        # API sometimes uses '{match}' or 'match' to denote variables
        return string.\
            replace('{{{}}}'.format(match), value).\
            replace(match, value)

    def get_urlpath(self, testconfig, path):
        """
        Gets a URL path
        where placeholders in given path are replaced by values from testconfig
        """
        urlpath = path
        for match in URLPATH_REPLACABLES:
            value = getattr(testconfig, match.lower())
            if value:
                urlpath = self.api_replace(urlpath, match, value)
        return urlpath

    def get_config(self, testmethod, testpath, testconfig_pk):
        """Gets test config from swagger and database"""
        urlpath = urllib.parse.unquote(testpath)
        config = {
            'found': False,
            'method': testmethod,
            'status_code': 200,
            'summary': 'Unknown',
            'urlpath': urlpath,
        }
        try:
            testconfig = TestConfiguration.objects.get(
                owner=self.request.user, pk=testconfig_pk)
        except TestConfiguration.DoesNotExist as err:
            raise PermissionDenied
        try:
            swagger = self.api.get_swagger(testconfig.api_version)
        except APIError as err:
            messages.error(self.request, err)
        else:
            for path, data in swagger['paths'].items():
                if path == urlpath and testmethod in data:
                    config.update({
                        'found': True,
                        'operation_id': data[testmethod]['operationId'],
                        'summary': data[testmethod]['summary'],
                        'urlpath': self.get_urlpath(testconfig, path),
                    })
        return config

    def run_test(self, config):
        """Runs a test with given config"""
        url = '{}{}'.format(settings.API_HOST, config['urlpath'])
        # Let APIError bubble up
        response = self.api.call(config['method'], url)
        try:
            text = response.json()
        except json.decoder.JSONDecodeError as err:
            text = response.text
        text = json.dumps(
            text, sort_keys=True, indent=2, separators=(',', ': '))
        result = {
            'text': text,
            'execution_time': response.execution_time,
            'status_code': response.status_code,
        }
        return result

    def get_context_data(self, **kwargs):
        context = super(RunView, self).get_context_data(**kwargs)
        self.api = API(self.request.session.get('obp'))
        config = self.get_config(**kwargs)
        context.update({
            'config': config,
            'text': None,
            'execution_time': -1,
            'messages': [],
            'success': False,
        })
        if not config['found']:
            msg = 'Unknown path {}!'.format(kwargs['testpath'])
            context['messages'].append(msg)
            return context

        try:
            result = self.run_test(config)
        except APIError as err:
            context['messages'].append(err)
            return context
        else:
            context.update(result)

        # Test if status code is as expected
        if result['status_code'] != config['status_code']:
            msg = 'Status code is {}, but expected {}!'.format(
                result['status_code'], config['status_code'])
            context['messages'].append(msg)
            return context

        context['success'] = True
        return context


class TestConfigurationCreateView(LoginRequiredMixin, CreateView):
    model = TestConfiguration
    form_class = TestConfigurationForm

    def form_valid(self, form):
        form.instance.owner = self.request.user
        return super(TestConfigurationCreateView, self).form_valid(form)

    def get_success_url(self):
        return reverse('runtests-index-testconfig', kwargs={
            'testconfig_pk': self.object.pk,
        })


class TestConfigurationUpdateView(LoginRequiredMixin, UpdateView):
    model = TestConfiguration
    form_class = TestConfigurationForm

    def get_object(self, **kwargs):
        object = super(TestConfigurationUpdateView, self).get_object(**kwargs)
        if self.request.user != object.owner:
            raise PermissionDenied
        return object

    def get_success_url(self):
        return reverse('runtests-index-testconfig', kwargs={
            'testconfig_pk': self.object.pk,
        })


class TestConfigurationDeleteView(LoginRequiredMixin, DeleteView):
    model = TestConfiguration
    form_class = TestConfigurationForm
    success_url = reverse_lazy('runtests-index')

    def get_object(self, **kwargs):
        object = super(TestConfigurationDeleteView, self).get_object(**kwargs)
        if self.request.user != object.owner:
            raise PermissionDenied
        return object
