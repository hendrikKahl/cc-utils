import datetime
import enum
import functools
import logging
import textwrap
import typing
import urllib.parse

import github3.repos

import gci.componentmodel as cm

import ccc.delivery
import ci.util
import cnudie.util
import concourse.model.traits.image_scan as image_scan
import delivery.client
import delivery.model
import github.compliance.issue
import github.compliance.milestone
import github.compliance.result as gcr
import model.delivery

logger = logging.getLogger(__name__)

_compliance_label_vulnerabilities = github.compliance.issue._label_bdba
_compliance_label_licenses = github.compliance.issue._label_licenses


def _criticality_label(classification: gcr.Severity):
    return f'compliance-priority/{classification.value}'


def _criticality_classification(cve_score: float) -> gcr.Severity:
    if not cve_score or cve_score <= 0:
        return None

    if cve_score < 4.0:
        return gcr.Severity.LOW
    if cve_score < 7.0:
        return gcr.Severity.MEDIUM
    if cve_score < 9.0:
        return gcr.Severity.HIGH
    if cve_score >= 9.0:
        return gcr.Severity.CRITICAL


def _delivery_dashboard_url(
    component: cm.Component,
    base_url: str,
):
    url = ci.util.urljoin(
        base_url,
        '#/component'
    )

    query = urllib.parse.urlencode(
        query={
            'name': component.name,
            'version': component.version,
            'view': 'bom',
        }
    )

    return f'{url}?{query}'


def _compliance_status_summary(
    component: cm.Component,
    resources: cm.Resource,
    report_urls: str,
    issue_description: str,
    issue_value: str,
):
    if isinstance(resources[0].type, enum.Enum):
        resource_type = resources[0].type.value
    else:
        resource_type = resources[0].type

    def pluralise(prefix: str, count: int):
        if count == 1:
            return prefix
        return f'{prefix}s'

    resource_versions = ', '.join((r.version for r in resources))

    report_urls = '\n- '.join(report_urls)

    summary = textwrap.dedent(f'''\
        # Compliance Status Summary

        |    |    |
        | -- | -- |
        | Component | {component.name} |
        | Component-Version | {component.version} |
        | Resource  | {resources[0].name} |
        | {pluralise('Resource-Version', len(resources))}  | {resource_versions} |
        | Resource-Type | {resource_type} |
        | {issue_description} | **{issue_value}** |

        The aforementioned {pluralise(resource_type, len(resources))} yielded findings
        relevant for future release decisions.

        For viewing detailed scan {pluralise('report', len(resources))}, see the following
        {pluralise('Scan Report', len(resources))}:
    ''')

    return summary + '- ' + report_urls


def _template_vars(
    result_group: gcr.ScanResultGroup,
    license_cfg: image_scan.LicenseCfg,
    delivery_dashboard_url: str='',
):
    component = result_group.component
    resource_name = result_group.resource_name
    resources = [res.resource for res in result_group.results]
    issue_type = result_group.issue_type

    results = result_group.results_with_findings

    resource_versions = ', '.join((r.resource.version for r in results))
    resource_types = ', '.join(set((r.resource.type.value for r in results)))

    template_variables = {
        'component_name': component.name,
        'component_version': component.version,
        'resource_name': resource_name,
        'resource_version': resource_versions,
        'resource_type': resource_types,
        'delivery_dashboard_url': delivery_dashboard_url,
    }

    if issue_type == _compliance_label_vulnerabilities:
        analysis_results = [r.result for r in results]
        greatest_cve = max((r.greatest_cve_score for r in results))
        template_variables['summary'] = _compliance_status_summary(
            component=component,
            resources=resources,
            issue_value=greatest_cve,
            issue_description='Greatest CVE Score',
            report_urls=[ar.report_url() for ar in analysis_results],
        )
        template_variables['greatest_cve'] = greatest_cve
        template_variables['criticality_classification'] = _criticality_classification(
            cve_score=greatest_cve
        ).value
    elif issue_type == _compliance_label_licenses:
        analysis_results = [r.result for r in results]
        prohibited_licenses = set()
        all_licenses = set()

        for r in results:
            all_licenses |= r.license_names

        for license_name in all_licenses:
            if not license_cfg.is_allowed(license_name):
                prohibited_licenses.add(license_name)

        template_variables['summary'] = _compliance_status_summary(
            component=component,
            resources=resources,
            issue_value=' ,'.join(prohibited_licenses),
            issue_description='Prohibited Licenses',
            report_urls=[ar.report_url() for ar in analysis_results],
        )
        template_variables['criticality_classification'] = gcr.Severity.CRITICAL.value
    else:
        raise NotImplementedError(issue_type)

    return template_variables


@functools.cache
def _target_milestone(
    repo: github3.repos.Repository,
    sprint: delivery.model.Sprint,
):
    return github.compliance.milestone.find_or_create_sprint_milestone(
        repo=repo,
        sprint=sprint,
    )


@functools.cache
def _target_sprint(
    delivery_svc_client: delivery.client.DeliveryServiceClient,
    latest_processing_date: datetime.date,
):
    target_sprint = delivery_svc_client.sprint_current(before=latest_processing_date)

    return target_sprint


def _latest_processing_date(
    cve_score: float,
    max_processing_days: image_scan.MaxProcessingTimesDays,
):
    return datetime.date.today() + datetime.timedelta(
        days=max_processing_days.for_cve(cve_score=cve_score),
    )


def create_or_update_github_issues(
    result_group_collection: gcr.ScanResultGroupCollection,
    max_processing_days: image_scan.MaxProcessingTimesDays,
    gh_api: github3.GitHub,
    overwrite_repository: github3.repos.Repository=None,
    preserve_labels_regexes: typing.Iterable[str]=(),
    github_issue_template_cfgs: list[image_scan.GithubIssueTemplateCfg]=None,
    delivery_svc_client: delivery.client.DeliveryServiceClient=None,
    delivery_svc_endpoints: model.delivery.DeliveryEndpointsCfg=None,
    license_cfg: image_scan.LicenseCfg=None, # XXX -> callback
):
    # workaround / hack:
    # we map findings to <component-name>:<resource-name>
    # in case of ambiguities, this would lead to the same ticket firstly be created, then closed
    # -> do not close tickets in this case.
    # a cleaner approach would be to create seperate tickets, or combine findings into shared
    # tickets. For the time being, this should be "good enough"

    result_groups = result_group_collection.result_groups
    result_groups_with_findings = result_group_collection.result_groups_with_findings
    result_groups_without_findings = result_group_collection.result_groups_without_findings

    err_count = 0

    def process_result(
        result_group: gcr.ScanResultGroup,
        action: str,
    ):
        nonlocal gh_api
        nonlocal err_count
        issue_type = result_group.issue_type

        if action == 'discard':
            results = result_group.results_without_findings
        elif action == 'report':
            results = result_group.results_with_findings

        # XXX delegate to rgroup
        if issue_type == _compliance_label_vulnerabilities:
            greatest_cve = max(results, key=lambda r: r.greatest_cve_score).greatest_cve_score
            criticality_classification = _criticality_classification(cve_score=greatest_cve)
        elif issue_type == _compliance_label_licenses:
            criticality_classification = gcr.Severity.CRITICAL

        if not len({r.component.name for r in results}) == 1:
            raise ValueError('not all component names are identical')

        component = result_group.component
        resources = [r.resource for r in results]
        resource = resources[0]

        # XXX this is bdba-specific
        analysis_results = [r.result for r in results]
        analysis_res = analysis_results[0]

        if overwrite_repository:
            repository = overwrite_repository
        else:
            source = cnudie.util.main_source(component=component)

            if not source.access.type is cm.AccessType.GITHUB:
                raise NotImplementedError(source)

            org = source.access.org_name()
            name = source.access.repository_name()
            gh_api = ccc.github.github_api(repo_url=source.access.repoUrl)

            repository = gh_api.repository(org, name)

        if action == 'discard':
            github.compliance.issue.close_issue_if_present(
                component=component,
                resource=resource,
                repository=repository,
                issue_type=issue_type,
            )

            logger.info(
                f'closed (if existing) gh-issue for {component.name=} {resource.name=} {issue_type=}'
            )
        elif action == 'report':
            if delivery_svc_client:
                assignees = delivery.client.github_users_from_responsibles(
                    responsibles=delivery_svc_client.component_responsibles(
                        component=component,
                        resource=resource,
                    ),
                    github_url=repository.url,
                )

                assignees = tuple((
                    u.username for u in assignees
                    if github.user.is_user_active(
                        username=u.username,
                        github=gh_api,
                    )
                ))

                try:
                    if issue_type == _compliance_label_vulnerabilities:
                        latest_processing_date = _latest_processing_date(
                            cve_score=greatest_cve,
                            max_processing_days=max_processing_days,
                        )
                    elif issue_type == _compliance_label_licenses:
                        # license issues are always "release-blockers"
                        latest_processing_date = datetime.date.today()
                    else:
                        raise NotImplementedError(issue_type)

                    target_sprint = _target_sprint(
                        delivery_svc_client=delivery_svc_client,
                        latest_processing_date=latest_processing_date,
                    )
                    target_milestone = _target_milestone(
                        repo=repository,
                        sprint=target_sprint,
                    )
                except Exception as e:
                    logger.warning(f'{e=}')
                    target_milestone = None
            else:
                assignees = ()
                target_milestone = None

            if isinstance(resource.type, enum.Enum):
                resource_type = resource.type.value
            else:
                resource_type = resource.type

            if delivery_svc_endpoints:
                delivery_dashboard_url = _delivery_dashboard_url(
                    component=component,
                    base_url=delivery_svc_endpoints.dashboard_url(),
                )
                delivery_dashboard_url = f'[Delivery-Dashboard]({delivery_dashboard_url})'
            else:
                delivery_dashboard_url = ''

            template_variables = _template_vars(
                result_group=result_group,
                license_cfg=license_cfg,
                delivery_dashboard_url=delivery_dashboard_url,
            )

            if github_issue_template_cfgs:
                for issue_cfg in github_issue_template_cfgs:
                    if issue_cfg.type == issue_type:
                        break
                else:
                    raise ValueError(f'no template for {issue_type=}')

                body = issue_cfg.body.format(**template_variables)
            else:
                body = textwrap.dedent(f'''\
                    # Compliance Status Summary

                    |    |    |
                    | -- | -- |
                    | Component | {component.name} |
                    | Component-Version | {component.version} |
                    | Resource  | {resource.name} |
                    | Resource-Version  | {resource.version} |
                    | Resource-Type | {resource_type} |
                    | Greatest CVSSv3 Score | **{greatest_cve}** |

                    The aforementioned {resource_type}, declared by the given content was found to
                    contain potentially relevant vulnerabilities.

                    See [scan report]({analysis_res.report_url()}) for both viewing a detailed
                    scanning report, and doing assessments (see below).

                    **Action Item**

                    Please take appropriate action. Choose either of:

                    - assess findings
                    - upgrade {resource_type} version
                    - minimise image

                    In case of systematic false-positives, consider adding scanning-hints to your
                    Component-Descriptor.
                '''
                )

            try:
                github.compliance.issue.create_or_update_issue(
                    component=component,
                    resource=resource,
                    issue_type=issue_type,
                    repository=repository,
                    body=body,
                    assignees=assignees,
                    milestone=target_milestone,
                    extra_labels=(
                        _criticality_label(classification=criticality_classification),
                    ),
                    preserve_labels_regexes=preserve_labels_regexes,
                )
            except github3.exceptions.GitHubError as ghe:
                err_count += 1
                logger.warning('error whilst trying to create or update issue - will keep going')
                logger.warning(f'error: {ghe} {ghe.code=} {ghe.message=}')

            logger.info(f'updated gh-issue for {component.name=} {resource.name=} {issue_type=}')
        else:
            raise NotImplementedError(action)

    for result_group in result_groups_with_findings:
        process_result(
            result_group=result_group,
            action='report',
        )

    for result_group in result_groups_without_findings:
        logger.info(f'discarding issue for {result_group.name=} vulnerabilities')
        process_result(
            result_group=result_group,
            action='discard',
        )

    if overwrite_repository:
        close_issues_for_absent_resources(
            result_groups=result_groups,
            repository=overwrite_repository,
            issue_type=None,
        )

    if err_count > 0:
        logger.warning(f'{err_count=} - there were errors - will raise')
        raise ValueError('not all gh-issues could be created/updated/deleted')


def close_issues_for_absent_resources(
    result_groups: list[gcr.ScanResultGroup],
    repository: github3.repos.Repository,
    issue_type: str | None,
):
    '''
    closes all open issues for component-resources that are not present in given result-groups.

    this is intended to automatically close issues for components or component-resources that
    have been removed from BoM.
    '''
    all_issues = github.compliance.issue.enumerate_issues(
        component=None,
        resource=None,
        repository=repository,
        issue_type=issue_type,
        state='open',
    )

    def component_resource_label(issue: github3.issues.Issue) -> str:
        for label in issue.labels():
            if label.name.startswith('ocm/resource'):
                return label.name

    component_resources_to_issues = {
        component_resource_label(issue): issue for issue in all_issues
    }

    for result_group in result_groups:
        resource_label = github.compliance.issue.resource_digest_label(
            component=result_group.component,
            resource=result_group.resource_name,
        )

        component_resources_to_issues.pop(resource_label, None)

    # any issues that have not been removed thus far were not referenced by given result_groups
    for issue in component_resources_to_issues.values():
        issue.create_comment('closing, because component/resource no longer present in BoM')
        issue.close()
