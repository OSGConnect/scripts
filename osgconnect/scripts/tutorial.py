#!/usr/bin/env python3

import configparser
import getopt
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import traceback
import urllib.error
import urllib.parse
import urllib.request

# We may augment this in runtime
ConfigFiles = ["/etc/ciconnect/config.ini"]

# Default configuration
Defaults = """
[connect]
brand = osg
errorsto = root

[tutorial]
localpaths = /stash/tutorials
github-paths = default

[collections]
default = CI-Connect/tutorial-, OSGConnect/tutorial-
"""

# GitHub limits API access for anonymous clients, so we'll authenticate
# to this service account just to extend API limits.
OAuthClient = {
    "clientid": "daf652bf17f603644de5",
    "secret": "8041808c74efe3090359b9c353a9d87e3e1d6c8f",
}


def githuburl(path, **params):
    if "://" not in path:
        path = "https://api.github.com" + path
    params["client_id"] = OAuthClient["clientid"]
    params["client_secret"] = OAuthClient["secret"]
    return path + "?" + "&".join(["%s=%s" % item for item in list(params.items())])


class mongodict(dict):
    def __setattr__(self, key, value):
        return self.__setitem__(key, value)

    def __getattr__(self, key):
        return self.__getitem__(key)


def send_exc(config):
    exc = traceback.format_exc()

    if os.path.exists("/usr/lib/sendmail"):
        fp = os.popen("/usr/lib/sendmail -t 2>/dev/null", "w")
    elif os.path.exists("/usr/sbin/sendmail"):
        fp = os.popen("/usr/sbin/sendmail -t 2>/dev/null", "w")
    else:
        return False

    import platform
    import pwd
    import socket
    import time

    rcpts = [x.strip() for x in config.get("connect", "errorsto").split(",")]
    ts = time.ctime()

    msg = []
    msg += ["To: " + ", ".join(rcpts)]
    msg += ["Subject: tutorial traceback"]
    msg += [""]
    msg += ["Python: " + sys.version]
    msg += ["System: " + str(platform.uname())]
    msg += ["User: " + pwd.getpwuid(os.getuid()).pw_name]
    msg += ["Hostname: " + socket.gethostname()]
    msg += ["Timestamp: " + ts]
    msg += [""]
    msg += [exc]

    fp.write("\n".join(msg))
    fp.close()
    return True


def get_repo(config, repo_url, location, branch="master", verbose=False):
    """
    Given a github repo url, download the appropriate tarball and extract

    :param repo_url: url to github repository
    :param location: path to place repo contents in
    :param branch:   branch from repo to grab, defaults to "master"

    :return: path to extracted directory, returns None if an error occurs
    """
    tarball_url = githuburl("{0}/tarball/{1}".format(repo_url, branch))
    if verbose:
        sys.stderr.write("Fetching tutorial from " + tarball_url + "\n")
    try:
        url_obj = urllib.request.urlopen(tarball_url)
        temp_obj = tempfile.TemporaryFile()
        shutil.copyfileobj(url_obj, temp_obj)
        extract_path = extract_tarfile(temp_obj, location)
        return extract_path
    except Exception as e:
        sys.stderr.write("Can't download files from github: %s\n" % str(e))
        if send_exc(config):
            sys.stderr.write("This error has been reported to system staff.\n")
        else:
            sys.stderr.write("Please report this error to your system staff.\n")


def extract_tarfile(file_object, location=None):
    """
    Extract a specified tarball to in a given directory

    :type file_object: file object
    :type location: str
    :param location: path where tarball should be extracted,
                    defaults to current directory
    :return: path to directory extracted from tarball
    """
    file_object.seek(0)  # need to go to beginning for tarfile
    tarball_obj = tarfile.open(fileobj=file_object)
    cur_dir = os.getcwd()
    if location is not None:
        (base_path, tutorial_dir) = os.path.split(location)
        os.chdir(base_path)
        # tarfile.extract doesn't appear to return an error if it can't
        # write, so let's proactively detect whether this is possible.
        if not os.access(".", os.R_OK):
            sys.stderr.write("You might not have write access to this directory.\n")
        extract_dir = os.path.join(base_path, tarball_obj.getmembers()[0].name)
    else:
        extract_dir = os.path.join(cur_dir, tarball_obj.getmembers()[0].name)

    tarball_obj.extractall()

    if location is not None:
        try:
            os.rename(extract_dir, tutorial_dir)
            extract_dir = os.path.join(location)
        except:
            pass

    os.chdir(cur_dir)
    return extract_dir


def get_tutorial_dir(tutorial):
    """
    Find first unused tutorial directory
    """
    if not tutorial.startswith("tutorial-"):
        tutorial = "tutorial-" + tutorial
    base_dir = os.path.join(".", tutorial)
    trial_dir = base_dir
    postfix = 0
    while os.path.exists(trial_dir):
        postfix += 1
        trial_dir = "%s.%d" % (base_dir, postfix)
    return base_dir, trial_dir


def get_tutorials(config):
    """
    Use github api to get a list of tutorials currently available
    """
    tutorials = {}

    for path in config.github_paths:
        if "/" in path:
            # separates org from repo name pattern
            org, pat = path.split("/", 1)
        else:
            # For now, pat is just a prefix.  We can make it a fnmatch
            # or re matching expr if needed.
            org, pat = path, "tutorial-"

        rx = re.compile('<([^>]+)>; rel="([^"]+)",* *')

        # need to retrieve paginated results
        nexturi = githuburl("/orgs/%s/repos" % org, per_page=20)
        while nexturi:
            try:
                github_page = urllib.request.urlopen(nexturi)
            except urllib.error.HTTPError as e:
                break

            repos = json.load(github_page)
            github_page.close()
            for repo in repos:
                if not repo["name"].startswith(pat):
                    continue
                name = repo["name"].replace(pat, "")
                burl = repo["branches_url"].replace("{/branch}", "")
                tutorials[name] = {
                    "description": repo["description"],
                    "url": repo["url"],
                    "branches_url": burl,
                }

            # This is ridiculous: github requires you to parse
            # a header like the following to get paged results metadata:
            # Link: <https://api.github.com/organizations/7956953/repos?page=2>; rel="next", <https://api.github.com/organizations/7956953/repos?page=2>; rel="last"

            nexturi = None
            header = github_page.info().get("Link")
            if not header:
                break
            for m in rx.finditer(header):
                link, rel = m.groups()
                if rel == "next":
                    nexturi = link

    # N.B. placement means that local has precedence
    for path in config.localpaths:
        path = os.path.join(path, config.branding)
        if not os.path.exists(path):
            continue
        for name in os.listdir(path):
            tut_location = os.path.join(path, name)
            if not os.path.isdir(tut_location):
                continue
            try:
                info = open(os.path.join(tut_location, ".info"), "r").readline().strip()
            except IOError:
                info = "???"
            tutorials[name] = {
                "description": info,
                "url": "file://{0}".format(tut_location),
                "branches_url": "",
            }  # need empty branches url for later

    return tutorials


def tutorial_branches(config, url):
    """
    Use github api to get all branches of a tutorial repo
    """
    if url.startswith("file://") or url == "":
        # no branches for file urls or missing urls
        return []
    try:
        jsontxt = urllib.request.urlopen(url)
    except urllib.error.HTTPError:
        return []
    branches = json.load(jsontxt)
    jsontxt.close()
    return [b["name"] for b in branches]


def get_collections(config, value):
    results = []
    for item in [x.strip() for x in value.split(",")]:
        if config.has_option("collections", item):
            results.extend(get_collections(config, config.get("collections", item)))
        else:
            results.append(item)
    return results


def connect_info(config):
    """
    Get connect details from connect script
    """

    simple = mongodict()
    simple.branding = ""
    simple.github_paths = []
    simple.localpaths = []

    if config.has_section("connect"):
        simple.branding = config.get("connect", "brand")

    # lists
    if config.has_option("tutorial", "localpaths"):
        simple.localpaths = [x.strip() for x in config.get("tutorial", "localpaths").split(",")]

    if config.has_option("tutorial", "github-paths"):
        simple.github_paths = get_collections(config, config.get("tutorial", "github-paths"))

    return simple


def initialize(dir):
    sys.stdout.write("Running setup in %s...\n" % dir)
    if not os.path.exists(os.path.join(dir, "setup")):
        return
    os.system('cd "%s" && ./setup || sh ./setup' % dir)


def main(args=None):
    """
    Run script and try to get and install correct tutorial files
    """

    if not args:
        args = sys.argv[1:]

    # Augment ConfigFiles if we can
    base = os.path.dirname(sys.argv[0])
    if base.endswith("/bin"):
        base = os.path.dirname(base)
    ConfigFiles.append(os.path.join(base, "ciconnect.ini"))
    ConfigFiles.append(os.path.join(base, "etc", "ciconnect.ini"))

    config = configparser.RawConfigParser()
    config.readfp(io.StringIO(Defaults))
    for fn in ConfigFiles:
        config.read(fn)

    def usage(fp=sys.stderr):
        p = os.path.basename(sys.argv[0])
        print("usage: %s list                 - show available tutorials" % p, file=fp)
        print("       %s info <tutorial-name> - show details of a tutorial" % p, file=fp)
        print("       %s <tutorial-name>      - set up a tutorial" % p, file=fp)
        return 2

    def listtutorials():
        if tutorials:
            longest = max([len(name) for name in tutorials])
            longest += 2
            for tutorial in sorted(tutorials.keys()):
                description = tutorials[tutorial]["description"]
                dots = "." * (longest - len(tutorial))
                sys.stdout.write("%s %s %s\n" % (tutorial, dots, description))
            sys.stdout.write("\n")
            sys.stdout.write(
                'Enter "tutorial name-of-tutorial" to clone and try out a tutorial.\n'
            )
        else:
            sys.stdout.write("No tutorials currently available.\n")
        return 0

    try:
        opts, args = getopt.getopt(args, "C:", ["collection="])
    except getopt.GetoptError as e:
        sys.stderr.write("error: %s\n" % str(e))
        return 2

    if "TUTORIAL_COLLECTION" in os.environ:
        config.set("tutorial", "github-paths", os.environ["TUTORIAL_COLLECTION"])

    for opt, arg in opts:
        if opt in ("-C", "--collection"):
            config.set("tutorial", "github-paths", arg)

    if not args:
        usage(fp=sys.stdout)
        print()
        args = ("list",)

    sconfig = connect_info(config)

    cmd = args[0]
    args = args[1:]

    if cmd == "list":
        tutorials = get_tutorials(sconfig)
        sys.stdout.write("Currently available tutorials: \n")
        listtutorials()
        return 0

    # provide info on a given tutorial
    if cmd == "info":
        tutorials = get_tutorials(sconfig)
        for t in args:
            if t not in tutorials:
                sys.stderr.write("Tutorial %s not found. Available tutorials are:\n" % t)
                listtutorials()
                return 10

        for t in args:
            print("Tutorial %s:" % t)
            print(tutorials[t]["description"])
        return 0

    # install tutorial
    else:
        tutorials = get_tutorials(sconfig)

        for tutorial in [cmd] + args:
            if tutorial.startswith("tutorial-"):
                tutorial = tutorial[9:]
            if tutorial not in tutorials:
                sys.stderr.write("Tutorial %s not found. Available tutorials are:\n" % tutorial)
                listtutorials()
                return 20

            base_dir, tutorial_dir = get_tutorial_dir(tutorial)
            branches = tutorial_branches(sconfig, tutorials[tutorial]["branches_url"])
            # print branches
            branch = None
            if sconfig.branding in branches:
                branch = sconfig.branding
            if branch:
                sys.stdout.write("Installing %s (%s)...\n" % (tutorial, branch))
            else:
                sys.stdout.write("Installing %s (master)...\n" % tutorial)
            if os.path.exists(base_dir):
                sys.stdout.write("Directory %s exists! " % base_dir)

            if tutorials[tutorial]["url"].startswith("file"):
                path = tutorials[tutorial]["url"][6:]
                try:
                    shutil.copytree(path, tutorial_dir)
                except shutil.Error:
                    sys.stderr.write("Can't write tutorial to {0}\n".format(tutorial_dir))
                    sys.exit(1)
            elif branch:
                if (
                    get_repo(config, tutorials[tutorial]["url"], tutorial_dir, branch=branch)
                    is None
                ):
                    return 1
            else:
                if get_repo(config, tutorials[tutorial]["url"], tutorial_dir) is None:
                    return 1
            sys.stdout.write("Tutorial files installed in %s.\n" % tutorial_dir)

            initialize(tutorial_dir)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
