#!/bin/env python
"""
Elliott is a CLI tool for managing Red Hat release advisories using the Erratatool
web service.
"""

# -----------------------------------------------------------------------------
# Module dependencies
# -----------------------------------------------------------------------------

# Prepare for Python 3
# stdlib
from __future__ import print_function
import datetime
from multiprocessing.dummy import Pool as ThreadPool
from multiprocessing import cpu_count
import os
import re

# ours
from ocp_cd_tools import Runtime
import ocp_cd_tools.constants
import ocp_cd_tools.bugzilla
import ocp_cd_tools.brew
import ocp_cd_tools.errata
import ocp_cd_tools.exceptions

# 3rd party
import click
import requests

# -----------------------------------------------------------------------------
# Constants and defaults
# -----------------------------------------------------------------------------
default_release_date = datetime.datetime(1970, 1, 1, 0, 0)
now = datetime.datetime.now()
YMD = '%Y-%m-%d'
pass_runtime = click.make_pass_decorator(Runtime)
context_settings = dict(help_option_names=['-h', '--help'])


@click.group(context_settings=context_settings)
@click.option("--metadata", "--metadata-dir", metavar='PATH', envvar="OIT_METADATA_DIR",
              default=None,
              help="Git repo or directory containing groups metadata directory if not current.")
@click.option("--working-dir", metavar='PATH', envvar="ELLIOTT_WORKING_DIR",
              default=None,
              help="Existing directory in which file operations should be performed.")
@click.option("--user", metavar='USERNAME', envvar="ELLIOTT_USER",
              default=None,
              help="Username for rhpkg.")
@click.option("--group", default=None, metavar='NAME',
              help="The group of images on which to operate.")
@click.option("--branch", default=None, metavar='BRANCH',
              help="Branch to override any default in group.yml.")
@click.option('--stage', default=False, is_flag=True, help='Force checkout stage branch for sources in group.yml.')
@click.option("-i", "--images", default=[], metavar='NAME', multiple=True,
              help="Name of group image member to include in operation (all by default). Can be comma delimited list.")
@click.option("-r", "--rpms", default=[], metavar='NAME', multiple=True,
              help="Name of group rpm member to include in operation (all by default). Can be comma delimited list.")
@click.option("-x", "--exclude", default=[], metavar='NAME', multiple=True,
              help="Name of group image or rpm member to exclude in operation (none by default). Can be comma delimited list.")
@click.option('--ignore-missing-base', default=False, is_flag=True, help='If a base image is not included, proceed and do not update FROM.')
@click.option('--latest-parent-version', default=False, is_flag=True,
              help='If a base image is not included, lookup latest FROM tag for parent. Implies --ignore-missing-base')
@click.option("--quiet", "-q", default=False, is_flag=True, help="Suppress non-critical output")
@click.option('--debug', default=False, is_flag=True, help='Show debug output on console.')
@click.option('--no_oit_comment', default=False, is_flag=True,
              help='Do not place OIT comment in Dockerfile. Can also be set in each config yaml')
@click.option("--source", metavar="ALIAS PATH", nargs=2, multiple=True,
              help="Associate a path with a given source alias.  [multiple]")
@click.option("--sources", metavar="YAML_PATH",
              help="YAML dict associating sources with their alias. Same as using --source multiple times.")
@click.pass_context
def cli(ctx, **kwargs):
    # @pass_runtime
    ctx.obj = Runtime(**kwargs)


# -----------------------------------------------------------------------------
# Utility Functions
# -----------------------------------------------------------------------------
def red_prefix(msg):
    """Print out a message prefix in bold red letters, like for "Error: "
messages"""
    click.secho(msg, nl=False, bold=True, fg='red')


def green_prefix(msg):
    """Print out a message prefix in bold red letters, like for "Success: "
messages"""
    click.secho(msg, nl=False, bold=True, fg='green')


def exit_unauthenticated():
    """Standard response when an API call returns 'unauthenticated' (401)"""
    red_prefix("Error Unauthenticated: ")
    click.echo("401 - user is not authenticated, are you sure you have a kerberos ticket?")
    exit(1)


def exit_unauthorized():
    """Standard response when an API call returns 'unauthorized' (403)"""
    red_prefix("Error Unauthorized: ")
    click.echo("403 - user is authenticated, but unauthorized to perform this action")
    exit(1)


def validate_release_date(ctx, param, value):
    """Ensures dates are provided in the correct format"""
    try:
        release_date = datetime.datetime.strptime(value, YMD)
        if release_date == default_release_date:
            # Default date, nothing special to note
            pass
        else:
            # User provided date passed validation, they deserve a
            # hearty thumbs-up!
            green_prefix("User provided release date: ")
            click.echo("{} - Validated".format(release_date.strftime(YMD)))
        return value
    except ValueError:
        raise click.BadParameter('Release date (--date) must be in YYYY-MM-DD format')

def validate_email_address(ctx, param, value):
    """Ensure that email addresses provided are valid email strings"""
    # Really just check to match /^[^@]+@[^@]+\.[^@]+$/
    email_re = re.compile('^[^@ ]+@[^@ ]+\.[^@ ]+$')
    if not email_re.match(value):
        raise click.BadParameter(
            "Invalid email address for {}: {}".format(param, value))

    return value

def release_from_branch(ver):
    """Parse the release version from the provided 'branch'.

For example, if --group=openshift-3.9 then runtime.group_config.branch
will have the value rhaos-3.9-rhel-7. When passed to this function the
return value would be the number 3.9, where in considering '3.9' then
'3.9' is the RELEASE version.

This behavior is HIGHLY dependent on the format of the input
argument. Hence, why this function indicates the results are based on
the 'branch' variable. Arbitrary input will fail. Use of this implies
you read the docs.
    """
    return ver.split('-')[1]


def major_from_branch(ver):
    """Parse the major version from the provided version (or 'branch').

For example, if --group=openshift-3.9 then runtime.group_config.branch
will have the value rhaos-3.9-rhel-7. When passed to this function the
return value would be the number 3, where in considering '3.9' then
'3' is the MAJOR version.

I.e., this gives you the X component if 3.9 => X.Y.

This behavior is HIGHLY dependent on the format of the input
argument. Hence, why this function indicates the results are based on
the 'branch' variable. Arbitrary input will fail. Use of this implies
you read the docs.
    """
    return ver.split('-')[1].split('.')[0]


def minor_from_branch(ver):
    """Parse the minor version from the provided version (or 'branch').

For example, if --group=openshift-3.9 then runtime.group_config.branch
will have the value rhaos-3.9-rhel-7. When passed to this function the
return value would be the number 9, where in considering '3.9' then
'9' is the MINOR version.

I.e., this gives you the Y component if 3.9 => X.Y.

This behavior is HIGHLY dependent on the format of the input
argument. Hence, why this function indicates the results are based on
the 'branch' variable. Arbitrary input will fail. Use of this implies
you read the docs.
    """
    return ver.split('-')[1].split('.')[1]


def pbar_header(msg_prefix='', msg='', seq=[], char='*'):
    """Generate a progress bar header for a given iterable or
sequence. The given sequence must have a countable length. A bar of
`char` characters is printed between square brackets.

    :param string msg_prefix: Header text to print in heavy green text
    :param string msg: Header text to print in the default char face
    :param sequence seq: A sequence (iterable) to size the progress
    bar against
    :param str char: The character to use when drawing the progress
    bar

For example:

    pbar_header("Foo: ", "bar", seq=[None, None, None], char='-')

would produce:

    Foo: bar
    [---]

where 'Foo: ' is printed using green_prefix() and 'bar' is in the
default console fg color and weight.

TODO: This would make a nice context wrapper.

    """
    green_prefix(msg_prefix)
    click.echo(msg)
    click.echo("[" + (char * len(seq)) + "]")


def progress_func(func, char='*'):
    """Use to wrap functions called in parallel. Prints a character for
each function call.

    :param lambda-function func: A 'lambda wrapped' function to call
    after printing a progress character
    :param str char: The character (or multi-char string, if you
    really wanted to) to print before calling `func`

    Usage examples:
      * See advisory:find-builds
    """
    click.secho(char, fg='green', nl=False)
    return func()


# -----------------------------------------------------------------------------
# CLI Commands - Please keep these in alphabetical order
# -----------------------------------------------------------------------------


#
# Set advisory state
# advisory:state
#
@cli.command("advisory:change-state", short_help="Change ADVISORY state")
@click.option("--state", '-s', type=click.Choice(['NEW_FILES', 'QE', 'REL_PREP']),
              help="New state for the Advisory. NEW_FILES, QE, REL_PREP.")
@click.argument('advisory', type=int)
@click.pass_context
def change_state(ctx, state, advisory):
    """Change the state of ADVISORY. Additional permissions may be
required to change an advisory to certain states.

An advisory may not move between some states until all criteria have
been met. For example, an advisory can not move from NEW_FILES to QE
unless Bugzilla Bugs or JIRA Issues have been attached.

See the advisory:find-bugs help for additional information on adding
Bugzilla Bugs.

    Move the advisory 123456 from NEW_FILES to QE state:

    $ elliott advisory:change-state --state QE 123456

    Move the advisory 123456 back to NEW_FILES (short option flag):

    $ elliott advisory:change-state -s NEW_FILES 123456
    """
    try:
        erratum = ocp_cd_tools.errata.get_erratum(advisory)
    except ocp_cd_tools.exceptions.ErrataToolUnauthenticatedException:
        exit_unauthenticated()

    click.echo("Changing state for {id} to {state}".format(id=advisory, state=state))
    click.echo(erratum)
    try:
        erratum.change_state(state)
    except ocp_cd_tools.exceptions.ErrataToolError as err:
        click.secho("Error changing state: ", nl=False, bold=True, fg='red')
        click.echo(str(err))
        exit(1)

    click.secho("Changed advisory state:", fg='green', bold=True)
    click.echo(erratum)


#
# Create Advisory (RPM and image)
# advisory:create
#
@cli.command("advisory:create", short_help="Create a new advisory")
@click.option("--kind", '-k', required=True,
              type=click.Choice(['rpm', 'image']),
              help="Kind of Advisory to create. Affects boilerplate text.")
@click.option("--impetus", default='standard',
              type=click.Choice(ocp_cd_tools.constants.errata_valid_impetus),
              help="Impetus for the advisory creation [standard, cve, ga, test]")
@click.option("--date", required=False,
              default=default_release_date.strftime(YMD),
              callback=validate_release_date,
              help="Release date for the advisory. Optional. Format: YYYY-MM-DD. Defaults to 3 weeks after the release with the highest date for that series")
@click.option('--assigned-to', metavar="EMAIL_ADDR", required=True,
              envvar="ELLIOTT_ASSIGNED_TO_EMAIL",
              callback=validate_email_address,
              help="The email address group to review and approve the advisory.")
@click.option('--manager', metavar="EMAIL_ADDR", required=True,
              envvar="ELLIOTT_MANAGER_EMAIL",
              callback=validate_email_address,
              help="The email address of the manager monitoring the advisory status.")
@click.option('--package-owner', metavar="EMAIL_ADDR", required=True,
              envvar="ELLIOTT_PACKAGE_OWNER_EMAIL",
              callback=validate_email_address,
              help="The email address of the person responsible managing the advisory.")
@click.option('--yes', '-y', is_flag=True,
              default=False, type=bool,
              help="Create the advisory (by default only a preview is displayed)")
@pass_runtime
@click.pass_context
def create(ctx, runtime, kind, impetus, date, assigned_to, manager, package_owner, yes):
    """Create a new advisory. The kind of advisory must be specified with
'--kind'. Valid choices are 'rpm' and 'image'.

    You MUST specify a group (ex: "openshift-3.9") manually using the
    --group option. See examples below.

New advisories will be created with a Release Date set to 3 weeks (21
days) from now. You may customize this (especially if that happens to
fall on a weekend) by providing a YYYY-MM-DD formatted string to the
--date option.

The default behavior for this command is to show what the generated
advisory would look like. The raw JSON used to create the advisory
will be printed to the screen instead of posted to the Errata Tool
API.

The impetus option only effects the metadata added to the new
advisory.

The --assigned-to, --manager and --package-owner options are required.
They are the email addresses of the parties responsible for managing and
approving the advisory.

Provide the '--yes' or '-y' option to confirm creation of the
advisory.

    PREVIEW an RPM Advisory 21 days from now (the default release date) for OSE 3.9:

    $ elliott --group openshift-3.9 advisory:create

    CREATE Image Advisory for the 3.5 series on the first Monday in March:

\b
    $ elliott --group openshift-3.5 advisory:create --yes -k image --date 2018-03-05
"""
    runtime.initialize(clone_distgits=False)
    minor = minor_from_branch(runtime.group_config.branch)

    if date == default_release_date.strftime(YMD):
        # User did not enter a value for --date, default is determined
        # by looking up the latest erratum in a series
        try:
            latest_advisory = ocp_cd_tools.errata.find_latest_erratum(kind, minor)
        except ocp_cd_tools.exceptions.ErrataToolUnauthenticatedException:
            exit_unauthenticated()
        except ocp_cd_tools.exceptions.ErrataToolUnauthorizedException:
            exit_unauthorized()
        except ocp_cd_tools.exceptions.ErrataToolError as err:
            red_prefix("Error searching advisories: ")
            click.echo(str(err))
            exit(1)
        else:
            if latest_advisory is None:
                red_prefix("No metadata discovered: ")
                click.echo("No advisory for 3.{y} has been released in recent history, can not auto determine next release date".format(
                    y=minor))
                exit(1)

        green_prefix("Found an advisory to calculate new release date from: ")
        click.echo("{synopsis} - {rel_date}".format(
            synopsis=latest_advisory.synopsis,
            rel_date=str(latest_advisory.release_date)))
        release_date = latest_advisory.release_date + datetime.timedelta(days=21)

        # We want advisories to issue on Tuesdays. Using strftime
        # Tuesdays are '2' with Sunday indexed as '0'
        day_of_week = int(release_date.strftime('%w'))
        if day_of_week != 2:
            # How far from our target day of the week?
            delta = day_of_week - 2
            release_date = release_date - datetime.timedelta(days=delta)
            click.secho("Adjusted release date to land on a Tuesday", fg='yellow')

        green_prefix("Calculated release date: ")
        click.echo("{}".format(str(release_date)))
    else:
        # User entered a valid value for --date, set the release date
        release_date = datetime.datetime.strptime(date, YMD)

    ######################################################################

    try:
        erratum = ocp_cd_tools.errata.new_erratum(
            kind=kind,
            release_date=release_date.strftime(YMD),
            create=yes,
            minor=minor,
            assigned_to=assigned_to,
            manager=manager,
            package_owner=package_owner
        )
    except ocp_cd_tools.exceptions.ErrataToolUnauthorizedException:
        exit_unauthorized()
    except ocp_cd_tools.exceptions.ErrataToolError as err:
        red_prefix("Error creating advisory: ")
        click.echo(str(err))
        exit(1)

    if yes:
        green_prefix("Created new advisory: ")
        click.echo(str(erratum.synopsis))

        # This is a little strange, I grant you that. For reference you
        # may wish to review the click docs
        #
        # http://click.pocoo.org/5/advanced/#invoking-other-commands
        #
        # You may be thinking, "But, add_metadata doesn't take keyword
        # arguments!" and that would be correct. However, we're not
        # calling that function directly. We actually use the context
        # 'invoke' method to call the _command_ (remember, it's wrapped
        # with click to create a 'command'). 'invoke' ensures the correct
        # options/arguments are mapped to the right parameters.
        ctx.invoke(add_metadata, kind=kind, impetus=impetus, advisory=erratum.advisory_id)
        click.echo(str(erratum))
    else:
        green_prefix("Would have created advisory: ")
        click.echo("JSON body displayed in full below")
        click.echo(erratum)


#
# Collect bugs
# advisory:find-bugs
#
@cli.command("advisory:find-bugs", short_help="Find or add MODIFED bugs to ADVISORY")
@click.option("--add", "-a", 'advisory',
              default=False, metavar='ADVISORY',
              help="Add found bugs to ADVISORY. Applies to bug flags as well (by default only a list of discovered bugs are displayed)")
@click.option("--auto",
              required=False,
              default=False, is_flag=True,
              help="AUTO mode, adds bugs based on --group")
@click.option("--id", type=int, metavar='BUGID',
              multiple=True, required=False,
              help="Bugzilla IDs to add, conflicts with --auto [MULTIPLE]")
@click.option("--flag", metavar='FLAG',
              required=False, multiple=True,
              help="Optional flag to apply to found bugs [MULTIPLE]")
@pass_runtime
def find_bugs(runtime, advisory, auto, id, flag):
    """Find Red Hat Bugzilla bugs or add them to ADVISORY. Bugs can be
"swept" into the advisory either automatically (--auto), or by
manually specifying one or more bugs using the --id option. Mixing
--auto with --id is an invalid use-case. The two use cases are
described below:

    Note: Using --id without --add is basically pointless

AUTOMATIC: For this use-case the --group option MUST be provided. The
--group automatically determines the correct target-releases to search

for MODIFIED bugs in.

MANUAL: The --group option is not required if you are specifying bugs
manually. Provide one or more --id's for manual bug addition.

    Automatically add bugs with target-release matching 3.7.Z or 3.7.0
    to advisory 123456:

\b
    $ elliott --group openshift-3.7 advisory:find-bugs --auto --add 123456

    List bugs that would be added to advisory 123456 and set the bro_ok flag on the bugs (NOOP):

\b
    $ elliott --group openshift-3.7 advisory:find-bugs --auto --flag bro_ok 123456

    Add two bugs to advisory 123456. Note that --group is not required
    because we're not auto searching:

\b
    $ elliott advisory:find-bugs --id 8675309 --id 7001337 --add 123456
"""
    if auto and len(id) > 0:
        raise click.BadParameter("Combining the automatic and manual bug attachment options is not supported")

    if auto:
        # Initialization ensures a valid group was provided
        runtime.initialize(clone_distgits=False)
        # Parse the Y component from the group version
        minor = minor_from_branch(runtime.group_config.branch)
        target_releases = ["3.{y}.z".format(y=minor), "3.{y}.0".format(y=minor)]
    elif len(id) == 0:
        # No bugs were provided
        raise click.BadParameter("If not using --auto then one or more --id's must be provided")

    if auto:
        bug_ids = ocp_cd_tools.bugzilla.search_for_bugs(target_releases)
    else:
        bug_ids = [ocp_cd_tools.bugzilla.Bug(id=i) for i in id]

    bug_count = len(bug_ids)

    if advisory is not False:
        try:
            advs = ocp_cd_tools.errata.get_erratum(advisory)
        except ocp_cd_tools.exceptions.ErrataToolUnauthenticatedException:
            exit_unauthenticated()

        if advs is False:
            red_prefix("Error: ")
            click.echo("Could not locate advisory {advs}".format(advs=advisory))
            exit(1)

        if auto:
            click.echo("Adding bugs to {advs} for target releases: {tr}".format(advs=advisory, tr=", ".join(target_releases)))
        else:
            click.echo("Adding {count} bugs to {advs}".format(count=bug_count, advs=advisory))

        if len(flag) > 0:
            for bug in bug_ids:
                bug.add_flags(flag)

        for res, bug in advs.add_bugs(bug_ids):
            if res.status_code == 201:
                green_prefix("Added bug: ")
                click.secho("  {id}".format(id=bug))
            else:
                red_prefix("Failed to add bug: ")
                click.echo("  {id} (rc={rc}, err={err})".format(id=bug, rc=res.status_code, err=res.text))
    # Add bug is false (noop)
    else:
        green_prefix("Would have added {n} bugs: ".format(n=bug_count))
        click.echo(", ".join([str(b) for b in bug_ids]))


#
# Attach Builds
# advisory:find-builds
#
@cli.command('advisory:find-builds',
             short_help='Find or attach builds to ADVISORY')
@click.option('--attach', '-a', 'advisory',
              default=False, metavar='ADVISORY',
              help='Attach the builds to ADVISORY (by default only a list of builds are displayed)')
@click.option('--build', '-b', 'builds',
              multiple=True, metavar='NVR_OR_ID',
              help='Add build NVR_OR_ID to ADVISORY [MULTIPLE]')
@click.option('--kind', '-k', metavar='KIND',
              required=True, type=click.Choice(['rpm', 'image']),
              help='Find builds of the given KIND [rpm, image]')
@pass_runtime
def find_builds(runtime, advisory, builds, kind):
    """Automatically or manually find or attach viable rpm or image builds
to ADVISORY. Default behavior searches Brew for viable builds in the
given group. Provide builds manually by giving one or more --build
(-b) options. Manually provided builds are verified against the Errata
Tool API.

\b
  * Attach the builds to ADVISORY by giving --attach
  * Specify the build type using --kind KIND

Example: Assuming --group=openshift-3.7, then a build is a VIABLE
BUILD IFF it meets ALL of the following criteria:

\b
  * HAS the tag in brew: rhaos-3.7-rhel7-candidate
  * DOES NOT have the tag in brew: rhaos-3.7-rhel7
  * IS NOT attached to ANY existing RHBA, RHSA, or RHEA

That is to say, a viable build is tagged as a "candidate", has NOT
received the "shipped" tag yet, and is NOT attached to any PAST or
PRESENT advisory. Here are some examples:

    SHOW the latest OSE 3.6 image builds that would be attached to a
    3.6 advisory:

    $ elliott --group openshift-3.6 advisory:find-builds -k image

    ATTACH the latest OSE 3.6 rpm builds to advisory 123456:

\b
    $ elliott --group openshift-3.6 advisory:find-builds -k rpm --attach 123456

    VERIFY (no --attach) that the manually provided RPM NVR and build
    ID are viable builds:

\b
    $ elliott --group openshift-3.6 advisory:find-builds -k rpm -b megafrobber-1.0.1-2.el7 -b 93170
"""
    runtime.initialize(clone_distgits=False)
    minor = minor_from_branch(runtime.group_config.branch)
    major = major_from_branch(runtime.group_config.branch)
    product_version = 'RHEL-7-OSE-{X}.{Y}'.format(X=major, Y=minor)
    base_tag = "rhaos-{major}.{minor}-rhel-7".format(major=major, minor=minor)

    # Test authentication
    try:
        ocp_cd_tools.errata.get_filtered_list(ocp_cd_tools.constants.errata_live_advisory_filter)
    except ocp_cd_tools.exceptions.ErrataToolUnauthenticatedException:
        exit_unauthenticated()

    session = requests.Session()

    if len(builds) > 0:
        green_prefix("Build NVRs provided: ")
        click.echo("Manually verifying the builds exist")
        try:
            unshipped_builds = [ocp_cd_tools.brew.get_brew_build(b, product_version, session=session) for b in builds]
        except ocp_cd_tools.exceptions.BrewBuildException as e:
            red_prefix("Error: ")
            click.echo(e)
            exit(1)
    else:
        if kind == 'image':
            initial_builds = runtime.image_metas()
            pbar_header("Generating list of {kind}s: ".format(kind=kind),
                        "Hold on a moment, fetching Brew buildinfo",
                        initial_builds)
            pool = ThreadPool(cpu_count())
            # Look up builds concurrently
            click.secho("[", nl=False)

            # Returns a list of (n, v, r) tuples of each build
            potential_builds = pool.map(
                lambda build: progress_func(lambda: build.get_latest_build_info(), '*'),
                initial_builds)
            # Wait for results
            pool.close()
            pool.join()
            click.echo(']')

            pbar_header("Generating build metadata: ",
                        "Fetching data for {n} builds ".format(n=len(potential_builds)),
                        potential_builds)
            click.secho("[", nl=False)

            # Reassign variable contents, filter out remove non_release builds
            potential_builds = [i for i in potential_builds
                                if i[0] not in runtime.group_config.get('non_release', [])]

            # By 'meta' I mean the lil bits of meta data given back from
            # get_latest_build_info
            #
            # TODO: Update the ImageMetaData class to include the NVR as
            # an object attribute.
            pool = ThreadPool(cpu_count())
            unshipped_builds = pool.map(
                lambda meta: progress_func(
                    lambda: ocp_cd_tools.brew.get_brew_build("{}-{}-{}".format(meta[0], meta[1], meta[2]),
                                                             product_version,
                                                             session=session),
                    '*'),
                potential_builds)
            # Wait for results
            pool.close()
            pool.join()
            click.echo(']')
        elif kind == 'rpm':
            green_prefix("Generating list of {kind}s: ".format(kind=kind))
            click.echo("Hold on a moment, fetching Brew builds")
            unshipped_build_candidates = ocp_cd_tools.brew.find_unshipped_build_candidates(
                base_tag,
                product_version,
                kind=kind)

            pbar_header("Gathering additional information: ", "Brew buildinfo is required to continue", unshipped_build_candidates)
            click.secho("[", nl=False)

            # We could easily be making scores of requests, one for each build
            # we need information about. May as well do it in parallel.
            pool = ThreadPool(cpu_count())
            results = pool.map(
                lambda nvr: progress_func(
                    lambda: ocp_cd_tools.brew.get_brew_build(nvr, product_version, session=session),
                    '*'),
                unshipped_build_candidates)
            # Wait for results
            pool.close()
            pool.join()
            click.echo(']')

            # We only want builds not attached to an existing open advisory
            unshipped_builds = [b for b in results if not b.attached_to_open_erratum]

    build_count = len(unshipped_builds)

    if advisory is not False:
        # Search and attach
        try:
            erratum = ocp_cd_tools.errata.get_erratum(advisory)
            erratum.add_builds(unshipped_builds)
            click.secho("Attached build(s) successfully", fg='green', bold=True)
        except ocp_cd_tools.exceptions.ErrataToolUnauthenticatedException:
            exit_unauthenticated()
        except ocp_cd_tools.exceptions.BrewBuildException as e:
            red_prefix("Error attaching builds: ")
            click.echo(str(e))
            exit(1)
    else:
        click.echo("The following {n} builds ".format(n=build_count), nl=False)
        click.secho("may be attached ", bold=True, nl=False)
        click.echo("to an advisory:")
        for b in sorted(unshipped_builds):
            click.echo(" " + b.nvr)


#
# Get an Advisory
# advisory:get
#
@cli.command("advisory:get", short_help="Get the ADVISORY")
@click.argument('advisory', type=int)
@click.option('--json', is_flag=True, default=False,
              help="Print the full JSON object of the advisory")
@click.pass_context
def get(ctx, json, advisory):
    """Get details about a specific advisory from the Errata Tool. By
default a brief one-line informational string is printed. Use the
--json option to fetch and print the full details of the advisory.

Fields for the short format: Release date, State, Synopsys, URL

    Basic one-line output for advisory 123456:

\b
    $ elliott advisory:get 123456
    2018-02-23T18:34:40 NEW_FILES OpenShift Container Platform 3.9 bug fix and enhancement update - https://errata.devel.redhat.com/advisory/123456

    Get the full JSON advisory object, use `jq` to print just the
    errata portion of the advisory:

\b
    $ elliott advisory:get --json 123456 | jq '.errata'
    {
      "rhba": {
        "actual_ship_date": null,
        "assigned_to_id": 3002255,
        "batch_id": null,
        ...
"""
    try:
        advisory = ocp_cd_tools.errata.get_erratum(advisory)
    except ocp_cd_tools.exceptions.ErrataToolUnauthenticatedException:
        exit_unauthenticated()

    if json:
        click.echo(advisory.to_json())
    else:
        click.echo(advisory)


#
# List Advisories (RPM and image)
# advisory:list
#
@cli.command("advisory:list", short_help="List filtered RHOSE advisories")
@click.option("--filter-id", '-f',
              default=ocp_cd_tools.constants.errata_default_filter,
              help="A custom filter id to list from")
@click.option("-n", default=5,
              help="Return only N latest results (default: 5)")
@click.option('--json', is_flag=True, default=False,
              help="Print the full JSON object of the advisory")
@click.pass_context
def list(ctx, filter_id, n, json):
    """Print a list of one-line informational strings of RHOSE
advisories. By default the 5 most recently created advisories are
printed. Note, they are NOT sorted by release date.

    NOTE: new filters must be created in the Errata Tool web
    interface.

Default filter definition: RHBA; Active; Product: RHOSE; Devel Group:
ENG OpenShift Enterprise; sorted by newest. Browse this filter
yourself online: https://errata.devel.redhat.com/filter/1965

    List 10 advisories instead of the default 5 with your custom
    filter #1337:

    $ elliott advisory:list -n 10 -f 1337
"""
    try:
        for erratum in ocp_cd_tools.errata.get_filtered_list(filter_id, limit=n):
            if json:
                click.echo(erratum.to_json())
            else:
                click.echo(erratum)
    except ocp_cd_tools.exceptions.ErrataToolUnauthenticatedException:
        exit_unauthenticated()
    except ocp_cd_tools.exceptions.ErrataToolError as err:
        red_prefix("Error: ")
        click.echo(str(err))
        exit(1)


#
# Add metadata comment to an Advisory
# advisory:add-metadata
#
@cli.command("advisory:add-metadata", short_help="Add metadata comment to an advisory")
@click.argument('advisory', type=int)
@click.option('--kind', '-k', required=True,
              type=click.Choice(['rpm', 'image']),
              help="KIND of advisory [rpm, image]")
@click.option('--impetus', default='standard',
              type=click.Choice(ocp_cd_tools.constants.errata_valid_impetus),
              help="Impetus for the advisory creation [standard, cve, ga, test]")
@pass_runtime
def add_metadata(runtime, kind, impetus, advisory):
    """Add metadata to an advisory. This is usually called by
advisory:create immediately after creation. It is only useful to you
if you are going back and adding metadata to older advisories.

    Note: Requires you provide a --group

Example to add standard metadata to a 3.10 images release

\b
    $ elliott --group=openshift-3.10 advisory:add-metadata --impetus standard --kind image
"""
    runtime.initialize(clone_distgits=False)
    release = release_from_branch(runtime.group_config.branch)

    try:
        advisory = ocp_cd_tools.errata.get_erratum(advisory)
    except ocp_cd_tools.exceptions.ErrataToolUnauthenticatedException:
        exit_unauthenticated()

    result = advisory.add_comment({'release': release, 'kind': kind, 'impetus': impetus})

    if result.status_code == 201:
        green_prefix("Added metadata successfully")
        click.echo()
    elif result.status_code == 403:
        exit_unauthorized()
    else:
        red_prefix("Something weird may have happened: ")
        click.echo("Unexpected response from ET API: {code}".format(
            code=result.status_code))
        exit(1)


#
# Find transitions
# bugzilla:find-transitions
#
@cli.command("bugzilla:find-transitions", short_help="Find bugs that have gone through the specifed transition")
@click.option("--currently", 'current_state',
              required=True,
              type=click.Choice(ocp_cd_tools.constants.VALID_BUG_STATES),
              help="State that the bug is in now")
@click.option("--from", 'changed_from',
              required=True,
              type=click.Choice(ocp_cd_tools.constants.VALID_BUG_STATES),
              help="State that the bug started in")
@click.option("--to", 'changed_to',
              required=True,
              type=click.Choice(ocp_cd_tools.constants.VALID_BUG_STATES),
              help="State that the bug ended in")
@click.option("--add-comment", "add_comment", is_flag=True, default=False,
              help="Add the nag comment to found bugs")
@pass_runtime
def find_transitions(runtime, current_state, changed_from, changed_to, add_comment):
    """Find Red Hat Bugzilla bugs that have gone through a specifed state change. This is mainly useful for
    finding "bad" state transitions to catch bugzilla users operating outside of a specified workflow.

\b
    $ elliott bugzilla:find-transitions --currently VERIFIED --from ASSIGNED --to ON_QA
"""
    bug_ids = ocp_cd_tools.bugzilla.search_for_bug_transitions(current_state, changed_from, changed_to)

    click.echo('Found the following bugs matching that transition: {}'.format(bug_ids))

    if(add_comment):
        # check if we've already commented
        for bug in bug_ids:
            if not bug.has_whiteboard_value('ocp_art_invalid_transition'):
                bug.add_comment(ocp_cd_tools.constants.bugzilla_invalid_transition_comment, is_private=True)
                click.echo('Added comment to {}'.format(bug.id))
                bug.add_whiteboard_value('ocp_art_invalid_transition')
            else:
                click.echo('Skipping {} because it has already been flagged.'.format(bug))

#
# Add a comment to a bug
# bugzilla:add-comment
#
@cli.command("bugzilla:add-comment", short_help="Add a comment to a bug")
@click.option("--id", "bug_ids",
              multiple=True, required=True,
              help="Bugzilla ID to add the comment to --auto [MULTIPLE]")
@click.option("--comment", "-c", "comment",
              required=True,
              help="Text of the added comment")
@click.option("--private", "is_private", is_flag=True, default=False,
              help="Make the added comment private")
@pass_runtime
def add_comment(runtime, bug_ids, comment, is_private):
    bug_list = [ocp_cd_tools.bugzilla.Bug(id=i) for i in bug_ids]
    click.echo(bug_list)

    for bug in bug_list:
        click.echo(bug)
        bug.add_comment(comment, is_private)

    click.echo('Added comment to {}'.format(bug.id))

# -----------------------------------------------------------------------------
# CLI Entry point
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    # Invoke the Click CLI wrapper function
    cli(obj={})
