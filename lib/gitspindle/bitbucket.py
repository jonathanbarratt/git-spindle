from gitspindle import *
from gitspindle.ansi import *
import gitspindle.bbapi as bbapi
import getpass
import glob
import os
import sys
import webbrowser
import binascii

class BitBucket(GitSpindle):
    prog = 'git bucket'
    what = 'BitBucket'
    spindle = 'bitbucket'
    hosts = ['bitbucket.org', 'www.bitbucket.org']
    api = bbapi

    def __init__(self):
        super(BitBucket, self).__init__()
        if self.use_credential_helper:
            # Git Credential Manager creates a token with too few scopes
            self.use_credential_helper = self.git('config', 'credential.helper').stdout.strip() != 'manager'

    # Support functions
    def login(self):
        passwordConfig = self.config('password')
        user, password = passwordConfig if isinstance(passwordConfig, tuple) else (None, passwordConfig)

        if not user:
            user = self.config('user')
        if not user:
            user = raw_input("BitBucket user: ").strip()
            if not user:
                print('Please do not specify an empty user')
                self.login()
                return
        self.config('user', user)

        if not password:
            password = getpass.getpass("BitBucket password for '%s': " % user)
            if not password:
                print('Please do not specify an empty password')
                self.login()
                return
            wrong_password = False
            try:
                self.bb = bbapi.Bitbucket(user, password)
                self.me = self.bb.user(user)
            except bbapi.BitBucketAuthenticationError:
                wrong_password = True
            if wrong_password:
                self.login()
                return
            self.config('password', password)
            location = '%s - do not share this file' % self.config_file
            if self.use_credential_helper:
                location = 'git\'s credential helper'
            print("Your BitBucket authentication password is now stored in %s" % location)

        try:
            self.bb = None
            self.me = None
            if not self.bb:
                self.bb = bbapi.Bitbucket(user, password)
            if not self.me:
                self.me = self.bb.user(user)
            self.my_login = self.me.username
            return
        except bbapi.BitBucketAuthenticationError:
            self.config('password', None)

        self.login()

    def parse_url(self, url):
        return ([self.my_login] + url.path.split('/'))[-2:]

    def get_repo(self, remote, user, repo):
        try:
            return self.bb.repository(user, repo)
        except bbapi.BitBucketError:
            pass

    def parent_repo(self, repo):
        if getattr(repo, 'is_fork', None):
            return self.bb.repository(repo.fork_of['owner'], repo.fork_of['slug'])

    def clone_url(self, repo, opts):
        if opts['--ssh'] or repo.is_private:
            return repo.links['clone']['ssh']
        if opts['--http']:
            return repo.links['clone']['https']
        if repo.owner['username'] == self.my_login:
            return repo.links['clone']['ssh']
        return repo.links['clone']['https']

    def api_root(self):
        return 'https://bitbucket.org/api/'

    # Commands
    @command
    def add_deploy_key(self, opts):
        """<key>...
           Add a deploy key"""
        repo = self.repository(opts)
        for arg in opts['<key>']:
            with open(arg) as fd:
                algo, key, title = (fd.read().strip().split(None, 2) + [None])[:3]
            key = "%s %s" % (algo, key)
            print("Adding deploy key %s" % arg)
            repo.add_deploy_key(key, title or key[:25])

    @command
    def add_privilege(self, opts):
        """[--admin|--read|--write] <user>...
           Add privileges for a user to this repo"""
        repo = self.repository(opts)
        priv = 'read'
        if opts['--admin']:
            priv = 'admin'
        elif opts['--write']:
            priv = 'write'
        for user in opts['<user>']:
            repo.add_privilege(user, priv)

    @command
    def add_public_keys(self, opts):
        """[<key>...]
           Adds keys to your public keys"""
        if not opts['<key>']:
            opts['<key>'] = glob.glob(os.path.join(os.path.expanduser('~'), '.ssh', 'id_*.pub'))
        existing = [x.key for x in self.me.keys()]
        for arg in opts['<key>']:
            with open(arg) as fd:
                algo, key, title = (fd.read().strip().split(None, 2) + [None])[:3]
            key = "%s %s" % (algo, key)
            if key in existing:
                continue
            print("Adding %s" % arg)
            self.me.create_key(label=title or key[:25], key=key)

    def find_fork(self, repo, user, exclude=None):
        forked = []
        # scan through the forks of the current repo
        for fork in repo.forks():
            if not exclude or fork.full_name != exclude:
                if fork.owner['username'] == user:
                    return fork
                # remember which forks have forks themselves
                if fork.forks_count:
                    forked.append(fork)
        # scan through the forks of the forks,
        # excluding the current repo to not follow it as parent
        for fork in forked:
            result = self.find_fork(fork, user, repo.full_name)
            if result:
                return result
        # scan through the parent repository if present,
        # excluding the current repo to not follow it as fork
        if hasattr(repo, 'parent') and repo.parent:
            parent = self.parent_repo(repo)
            if not exclude or parent.full_name != exclude:
                if parent.owner['username'] == user:
                    return parent
                return self.find_fork(parent, user, repo.full_name)

    @command
    def add_remote(self, opts):
        """[--ssh|--http] <user_or_repo> [<name>]
           Add user's fork or arbitrary repo (containing slash) as a named remote. The name defaults to the user's loginname"""
        if '/' in opts['<user_or_repo>']:
            opts['<repo>'] = opts['<user_or_repo>']
            fork = self.repository(opts)
        else:
            fork = self.find_fork(self.repository(opts), opts['<user_or_repo>'])

        if not fork:
            err('Fork of user "%s" does not exist' % opts['<user_or_repo>'])

        url = self.clone_url(fork, opts)
        name = opts['<name>'] or fork.owner['username']
        self.gitm('remote', 'add', name, url, redirect=False)
        self.gitm('fetch', '--tags', name, redirect=False)

    @command
    def apply_pr(self, opts):
        """[--parent] <pr-number>
           Applies a pull request as a series of cherry-picks"""
        repo = self.repository(opts)
        try:
            pr = repo.pull_request(opts['<pr-number>'])
        except bbapi.BitBucketError:
            err("Error while retrieving pull request #%s: %s" % (opts['<pr-number>'], sys.exc_info()[1]))
        if not pr:
            err("Pull request %s does not exist" % opts['<pr-number>'])
        print("Applying pull request #%d from %s: %s" % (pr.id, pr.author['display_name'] or pr.author['username'], pr.title))
        # Warnings
        warned = False
        cbr = self.git('symbolic-ref', 'HEAD').stdout.strip().replace('refs/heads/', '')
        if cbr != pr.destination['branch']['name']:
            print(wrap("Pull request was filed against '%s', but you are %s" % (pr.destination['branch']['name'], "on branch '%s'" % cbr if cbr else "in 'detached HEAD' state"), fgcolor.red))
            warned = True
        if pr.state == 'MERGED':
            print(wrap("Pull request was already merged by %s" % (pr.closed_by['display_name'] or pr.closed_by['username']), fgcolor.red))
            warned = True
        if pr.state == 'DECLINED':
            print(wrap("Pull request has already been declined: %s" % pr.reason, fgcolor.red))
            warned = True
        if warned:
            if not self.question("Continue?", default=False):
                sys.exit(1)
        # Fetch PR if needed
        pull_ref = 'refs/remotes/%s/pull-requests/%d' % (opts['--parent'] and 'upstream' or 'origin', pr.id)
        sha = self.git('rev-parse', '--verify', '--quiet', pull_ref).stdout.strip()
        if not sha.startswith(pr.source['commit']['hash']):
            print('Fetching pull request')
            url = self.bb.repository(*pr.source['repository']['full_name'].split('/')).links['clone']['https']
            source_branch_ref = 'refs/heads/%s' % pr.source['branch']['name']
            remote_refs = [x.split('\t', 1)[1] for x in self.gitm('ls-remote', '--refs', '-q', '-h', '-t', url).stdout.strip().split('\n')]
            remote_refs.remove(source_branch_ref)
            remote_refs = [source_branch_ref] + remote_refs
            for remote_ref in remote_refs:
                self.gitm('fetch', url, remote_ref, redirect=False)
                self.git('update-ref', pull_ref, pr.source['commit']['hash'])
                sha = self.git('rev-parse', '--verify', '--quiet', pull_ref).stdout.strip()
                if sha.startswith(pr.source['commit']['hash']):
                    break
        if not sha.startswith(pr.source['commit']['hash']):
            err("Cannot find pull request commit in current heads and tags")
        head_sha = self.gitm('rev-parse', '--verify', '--quiet', 'HEAD').stdout.strip()
        merge_base = self.git('merge-base', pr.source['commit']['hash'], head_sha).stdout.strip()
        if merge_base.startswith(pr.source['commit']['hash']):
            print("Pull request was already merged into this history")
        elif merge_base == head_sha:
            print("Fast-forward merging %s..%s" % (pr.destination['branch']['name'], pull_ref))
            self.gitm('merge', '--ff-only', pull_ref, redirect=False)
        else:
            print("Cherry-picking %s..%s" % (pr.destination['branch']['name'], pull_ref))
            self.gitm('cherry-pick', '%s..%s' % (pr.destination['branch']['name'], pull_ref), redirect=False)

    @command
    def browse(self, opts):
        """[--parent] [--no-browser] [<repo>] [<section>]
           Open the GitHub page for a repository in a browser"""
        sections = ['src', 'commits', 'branches', 'pull-requests', 'downloads', 'admin', 'issues', 'wiki']
        if opts['<repo>'] in sections and not opts['<section>']:
            opts['<repo>'], opts['<section>'] = None, opts['<repo>']
        repo = self.repository(opts)
        url = repo.links['html']['href']
        if opts['<section>']:
            url += '/' + opts['<section>']
        if opts['--no-browser'] or self.git('config', '--bool', 'gitspindle.no-browser').stdout.strip() == 'true':
            print('Please open the URL %s in your browser' % url)
        else:
            webbrowser.open_new(url)

    @command
    def cat(self, opts):
        """<file>...
           Display the contents of a file on BitBucket"""
        for arg in opts['<file>']:
            repo, ref, file = ([None, None] + arg.split(':',2))[-3:]
            user = None
            if repo:
                user, repo = ([None] + repo.split('/'))[-2:]
                repo = self.bb.repository(user or self.my_login, repo)
            else:
                repo = self.repository(opts)
                file = self.rel2root(file)
            try:
                content = repo.src(path=file, revision=ref or 'master') # BitBucket has no API to retrieve the default branch
            except bbapi.BitBucketError:
                err("No such file: %s" % arg)
            if not hasattr(content, '_data'):
                err("Not a regular file: %s" % arg)
            if getattr(content, 'encoding', None) == 'base64':
                os.write(sys.stdout.fileno(), binascii.a2b_base64(content._data))
            else:
                os.write(sys.stdout.fileno(), content._data.encode('utf-8'))

    @command
    def clone(self, opts, repo=None):
        """[--ssh|--http] [--triangular [--upstream-branch=<branch>]] [--parent] [git-clone-options] <repo> [<dir>]
           Clone a repository by name"""
        if not repo:
            repo = self.repository(opts)
        url = self.clone_url(repo, opts)

        args = opts['extra-opts']
        args.append(url)
        dir = opts['<dir>'] and opts['<dir>'][0] or repo.name
        if '--bare' in args:
            dir += '.git'
        args.append(dir)

        self.gitm('clone', *args, redirect=False).returncode
        os.chdir(dir)
        self.set_origin(opts, repo=repo)

    @command
    def create(self, opts):
        """[--private] [--team=<team>/<project>] [--description=<description>]
           Create a repository on bitbucket to push to"""
        root = self.gitm('rev-parse', '--show-toplevel').stdout.strip()
        name = os.path.basename(root)
        project = None
        if opts['--team']:
            team, project = (opts['--team'].split('/', 1) + [None])[:2]
            if not project:
                err('When creating a repository for a team, you also need to specify the project')
            dest = self.bb.team(team)
            try:
                dest.project(project)
            except bbapi.BitBucketError:
                err('Project with key "%s" does not exist for team "%s"' % (project, team))
        else:
            dest = self.me
        try:
            dest.repository(name)
            err("Repository already exists")
        except bbapi.BitBucketError:
            pass

        repo = dest.create_repository(slug=name, description=opts['--description'], is_private=opts['--private'], has_issues=True, has_wiki=True, project=project)
        if 'origin' in self.remotes():
            print("Remote 'origin' already exists, adding the BitBucket repository as 'bitbucket'")
            self.set_origin(opts, repo=repo, remote='bitbucket')
        else:
            self.set_origin(opts, repo=repo)

    @command
    def deploy_keys(self, opts):
        """[<repo>]
           Lists all keys for a repo"""
        repo = self.repository(opts)
        for key in repo.deploy_keys():
            print("%s %s (id: %s)" % (key['key'], key.get('label', ''), key['pk']))

    @command
    def fetch(self, opts):
        """[--ssh|--http] <user> [<refspec>]
           Fetch refs from a user's fork"""
        for fork in self.repository(opts).forks():
            if fork.owner['username'] in opts['<user>']:
                url = self.clone_url(fork, opts)
                refspec = opts['<refspec>'] or 'refs/heads/*'
                if ':' not in refspec:
                    if not refspec.startswith('refs/'):
                        refspec += ':' + 'refs/remotes/%s/' % fork.owner['username'] + refspec
                    else:
                        refspec += ':' + refspec.replace('refs/heads/', 'refs/remotes/%s/' % fork.owner['username'])
                self.gitm('fetch', url, refspec, redirect=False)

    @command
    def fork(self, opts):
        """[--ssh|--http] [--triangular [--upstream-branch=<branch>]] [<repo>]
           Fork a repo and clone it"""
        do_clone = bool(opts['<repo>'])
        repo = self.repository(opts)
        if repo.owner['username'] == self.my_login:
            err("You cannot fork your own repos")

        try:
            self.me.repository(repo.name)
            err("Repository already exists")
        except bbapi.BitBucketError:
            pass

        my_fork = repo.fork()

        if do_clone:
            self.clone(opts, repo=my_fork)
        else:
            self.set_origin(opts, repo=my_fork)

    def list_forks(self, repo, recursive=True):
        for fork in repo.forks():
            print("[%s] %s" % (fork.owner['username'], fork.links['html']['href']))
            if recursive and fork.forks_count:
                self.list_forks(fork)

    @command
    def forks(self, opts):
        """[--parent|--root] [--recursive] [<repo>]
           List all forks of this repository"""
        repo = self.repository(opts)
        print("[%s] %s" % (wrap(repo.owner['username'], attr.bright), repo.links['html']['href']))
        self.list_forks(repo, opts['--recursive'])

    @command
    def invite(self, opts):
        """[--read|--write|--admin] <email>...
           Invite users to collaborate on this repository"""
        repo = self.repository(opts)
        priv = 'read'
        if opts['--admin']:
            priv = 'admin'
        elif opts['--write']:
            priv = 'write'
        for email in opts['<email>']:
            invitation = repo.invite(email, priv)
            print("Invitation with %s privileges sent to %s" % (invitation['permission'], invitation['email']))

    @command
    def issue(self, opts):
        """[<repo>] [--parent] [--message=<message>|--file=<file>|--template=<file>|--reuse-message=<commit>] [--edit] [<issue>...]
           Show issue details or report an issue"""
        if opts['<repo>'] and opts['<repo>'].isdigit():
            # Let's assume it's an issue
            opts['<issue>'].insert(0, opts['<repo>'])
            opts['<repo>'] = None
        repo = self.repository(opts)
        for issue_no in opts['<issue>']:
            try:
                issue = repo.issue(issue_no)
                print(wrap(issue.title, attr.bright, attr.underline))
                print(issue.content['raw'])
                print(issue.html_url)
            except bbapi.BitBucketError:
                bbe = sys.exc_info()[1]
                if bbe.args[0] == 'No Issue matches the given query.':
                    print('No issue with id %s found in repository %s' % (issue_no, repo.full_name))
                else:
                    raise
        if not opts['<issue>']:
            found, edit, template, message = self.determine_message(opts)
            if not found:
                edit = True
                message = """
# Reporting an issue on %s/%s
# Please describe the issue as clearly as possible. Lines starting with '#' will
# be ignored, the first line will be used as title for the issue.
#""" % (repo.owner['username'], repo.name)

            if edit:
                message = self.edit_msg(message, 'ISSUE_EDITMSG', False)

            title, body = (message + '\n').split('\n', 1)
            title = title.strip()
            body = body.strip()

            if not body:
                err("No issue message specified")

            if template and message == template:
                err("Template file was not changed")

            try:
                issue = repo.create_issue(title=title, body=body)
                print("Issue %d created %s" % (issue.id, issue.html_url))
            except:
                filename = self.backup_message(title, body, 'issue-message-')
                err("Failed to create an issue, the issue text has been saved in %s" % filename)

    @command
    def issues(self, opts):
        """[<repo>] [--parent] [<query>]
           List issues in a repository"""
        if opts['<repo>'] and not opts['<query>'] and '=' in opts['<repo>']:
            # Let's assume it's a query
            opts['<query>'] = opts['<repo>']
            opts['<repo>'] = None
        if not opts['<repo>'] and not self.in_repo:
            repos = self.me.repositories()
        else:
            # the parent is already retrieved in the for loop below
            # without this, you get the grandparent instead if there is one
            tmpOpts = dict(opts)
            tmpOpts['--parent'] = False
            repos = [self.repository(tmpOpts)]
        for repo in repos:
            repo = (opts['--parent'] and self.parent_repo(repo)) or repo
            query = opts['<query>']
            if query:
                if not 'state' in query:
                    query = '(state != "resolved" AND state != "invalid" AND state != "duplicate" AND state != "wontfix" AND state != "closed") AND (%s)' % query
            else:
                query = 'state != "resolved" AND state != "invalid" AND state != "duplicate" AND state != "wontfix" AND state != "closed"'
            try:
                issues = repo.issues(query)
            except bbapi.BitBucketError:
                issues = None
            try:
                pullrequests = repo.pull_requests()
            except bbapi.BitBucketError:
                pullrequests = None

            if issues:
                print(wrap("Issues for %s" % repo.full_name, attr.bright))
                for issue in issues:
                    print("[%d] %s %s" % (issue.id, issue.title, issue.html_url))
            if pullrequests:
                print(wrap("Pull requests for %s" % repo.full_name, attr.bright))
                for pr in pullrequests:
                    print("[%d] %s %s" % (pr.id, pr.title, pr.html_url))


    @command
    def ls(self, opts):
        """[<dir>...]
           Display the contents of a directory on BitBucket"""
        for arg in opts['<dir>'] or ['']:
            repo, ref, file = ([None, None] + arg.split(':',2))[-3:]
            user = None
            if repo:
                user, repo = ([None] + repo.split('/'))[-2:]
                repo = self.bb.repository(user or self.my_login, repo)
            else:
                repo = self.repository(opts)
                file = self.rel2root(file)
            try:
                content = repo.src(path=file or '/', revision=ref or 'master') # BitBucket has no API to retrieve the default branch
            except bbapi.BitBucketError:
                err("No such file: %s" % arg)
            if hasattr(content, '_data'):
                err("Not a directory: %s" % arg)
            content = content.files + [{'path': x, 'size': 0, 'revision': '', 'type': 'dir'} for x in content.directories]
            content.sort(key=lambda x: x['path'])
            mt = max([len(file.get(type, 'file')) for file in content])
            ms = max([len(str(file['size'])) for file in content])
            fmt = "%%(type)-%ds %%(size)-%ds %%(revision)7.7s %%(path)s" % (mt, ms)
            for file in content:
                if 'type' not in file:
                    file['type'] = 'file'
                print(fmt % file)

    @command
    def mirror(self, opts):
        """[--ssh|--http] [--goblet] [<repo>]
           Mirror a repository, or all repositories for a user"""
        if opts['<repo>'] and opts['<repo>'].endswith('/*'):
            user = opts['<repo>'].rsplit('/', 2)[-2]
            for repo in self.bb.user(user).repositories():
                opts['<repo>'] = repo.full_name
                self.mirror(opts)
            return
        repo = self.repository(opts)
        git_dir = repo.name + '.git'
        cur_dir = os.path.basename(os.path.abspath(os.getcwd()))
        if cur_dir != git_dir and not os.path.exists(git_dir):
            url = self.clone_url(repo, opts)
            self.gitm('clone', '--mirror', url, git_dir, redirect=False)
        else:
            if git_dir == cur_dir:
                git_dir = '.'
            # Update the current, mirrored repo
            if self.git('--git-dir', git_dir, 'config', 'core.bare').stdout.strip() != 'true' or \
               self.git('--git-dir', git_dir, 'config', 'remote.origin.mirror').stdout.strip() != 'true':
                   err("This is not a mirrored repository")
            self.gitm('--git-dir', git_dir, 'fetch', '-q', '--prune', 'origin', redirect=False)

        with open(os.path.join(git_dir, 'description'), 'w') as fd:
            if PY3:
                fd.write(repo.description or "")
            else:
                fd.write((repo.description or "").encode('utf-8'))
        if opts['--goblet']:
            cwd = os.getcwd()
            os.chdir(git_dir)
            self.setup_goblet(opts)
            os.chdir(cwd)

    @command
    def privileges(self, opts):
        """[<repo>]
           List repo privileges"""
        repo = self.repository(opts)
        order = {'admin': 0, 'write': 1, 'read': 2}
        privs = repo.privileges()
        if not privs:
            return
        privs.sort(key=lambda priv: (order[priv['privilege']], priv['user']['username']))
        maxlen = max([len(priv['user']['username']) for priv in privs])
        fmt = "%%s %%-%ds (%%s)" % maxlen
        for priv in privs:
            print(fmt % (wrap("%-5s" % priv['privilege'], attr.faint), priv['user']['username'], priv['user']['display_name']))

    @command
    def public_keys(self, opts):
        """[<user>]
           Lists all keys for a user"""
        user = opts['<user>'] and self.bb.user(opts['<user>'][0]) or self.me
        for key in user.keys():
            print("%s %s" % (key.key, key.label or ''))

    @command
    def pull_request(self, opts):
        """[--message=<message>|--file=<file>|--template=<file>|--reuse-message=<commit>] [--edit] [--yes] [<yours:theirs>]
           Opens a pull request to merge your branch to an upstream branch"""
        repo = self.repository(opts)
        if repo.is_fork:
            parent = self.parent_repo(repo)
        else:
            parent = repo
        # Which branch?
        src = opts['<yours:theirs>'] or ''
        dst = None
        if ':' in src:
            src, dst = src.split(':', 1)
        if not src:
            src = self.gitm('rev-parse', '--abbrev-ref', 'HEAD').stdout.strip()
        if not dst:
            tracking_remote = None
            tracking_branch = self.git('rev-parse', '--abbrev-ref', '%s@{u}' % src).stdout.strip()
            if '/' in tracking_branch:
                tracking_remote, tracking_branch = tracking_branch.split('/', 1)
            if (parent == repo and tracking_remote == 'origin') or (parent != repo and tracking_remote == 'upstream'):
                dst = tracking_branch
            else:
                dst = parent.main_branch()

        if src == dst and parent == repo:
            err("Cannot file a pull request on the same branch")

        # Try to get the local commit
        commit = self.gitm('show-ref', 'refs/heads/%s' % src).stdout.split()[0]
        # Do they exist on bitbucket?
        srcb = repo.branches().get(src, None)
        if not srcb:
            if self.question("Branch %s does not exist in your BitBucket repo, shall I push?" % src):
                self.gitm('push', repo.remote, src, redirect=False)
                srcb = repo.branches().get(src, None)
            else:
                err("Aborting")
        elif srcb and srcb.raw_node != commit:
            # Have we diverged? Then there are commits that are reachable from the github branch but not local
            diverged = self.gitm('rev-list', srcb.raw_node, '^' + commit)
            if diverged.stderr or diverged.stdout:
                if self.question("Branch %s has diverged from github, shall I push and overwrite?" % src, default=False):
                    self.gitm('push', '--force', repo.remote, src, redirect=False)
                else:
                    err("Aborting")
            else:
                if self.question("Branch %s not up to date on github, but can be fast forwarded, shall I push?" % src):
                    self.gitm('push', repo.remote, src, redirect=False)
                else:
                    err("Aborting")
            srcb = repo.branches().get(src, None)

        dstb = parent.branches().get(dst, None)
        if not dstb:
            err("Branch %s does not exist in %s/%s" % (dst, parent.owner.login, parent.name))

        # Do we have the dst locally?
        for remote in self.gitm('remote').stdout.strip().split("\n"):
            url = self.gitm('config', 'remote.%s.url' % remote).stdout.strip()
            if url in parent.links['clone'].values():
                if parent.is_private and url != parent.links['clone']['ssh']:
                    err("You should configure %s to fetch via ssh, it is a private repo" % parent.full_name)
                self.gitm('fetch', remote, redirect=False)
                break
        else:
            err("You don't have %ss configured as a remote repository" % parent.full_name)

        # How many commits?
        accept_empty_body = False
        commits = try_decode(self.gitm('log', '--pretty=format:%H', '%s/%s..%s' % (remote, dst, src)).stdout).split()
        commits.reverse()
        if not commits:
            err("Your branch has no commits yet")

        found, edit, template, message = self.determine_message(opts)
        if not found:
            edit = True
            # 1 commit: title/body from commit
            if len(commits) == 1:
                title, body = self.gitm('log', '-1', '--pretty=format:%s\n%b', commits[0]).stdout.split('\n', 1)
                title = title.strip()
                body = body.strip()
                accept_empty_body = not bool(body)

            # More commits: title from branchname (titlecased, s/-/ /g), body comments from shortlog
            else:
                title = src
                if '/' in title:
                    title = title[title.rfind('/') + 1:]
                title = title.title().replace('-', ' ')
                body = ""

            message = "%s\n\n%s" % (title, body)

        if edit:
            message += """
# Requesting a pull from %s/%s into %s/%s
#
# Please enter a message to accompany your pull request. Lines starting
# with '#' will be ignored, and an empty message aborts the request.
#""" % (repo.owner['username'], src, parent.owner['username'], dst)
            message += "\n# " + try_decode(self.gitm('shortlog', '%s/%s..%s' % (remote, dst, src)).stdout).strip().replace('\n', '\n# ')
            message += "\n#\n# " + try_decode(self.gitm('diff', '--stat', '%s^..%s' % (commits[0], commits[-1])).stdout).strip().replace('\n', '\n#')
            message = self.edit_msg(message, 'PULL_REQUEST_EDIT_MSG', False)

        title, body = (message + '\n').split('\n', 1)
        title = title.strip()
        body = body.strip()

        if not body and not accept_empty_body:
            err("No pull request message specified")

        if template and message == template:
            err("Template file was not changed")

        try:
            pull = parent.create_pull_request(src=srcb, dst=dstb, title=title, body=body)
            print("Pull request %d created %s" % (pull.id, pull.links['html']['href']))
        except:
            filename = self.backup_message(title, body, 'pull-request-message-')
            err("Failed to create a pull request, the pull request text has been saved in %s" % filename)

    @command
    def remove_deploy_key(self, opts):
        """<key>...
           Remove deploy key by id"""
        repo = self.repository(opts)
        for key in opts['<key>']:
            repo.remove_deploy_key(key)

    @command
    def remove_privilege(self, opts):
        """<user>...
           Remove a user's privileges"""
        repo = self.repository(opts)
        for user in opts['<user>']:
            repo.remove_privilege(user)

    @command
    def repos(self, opts):
        """[--no-forks] [<user>]
           List all repos of a user, by default yours"""
        try:
            repos = self.bb.user(opts['<user>'] or self.my_login).repositories()
        except bbapi.BitBucketError:
            if 'is a team account' in str(sys.exc_info()[1]):
                repos = self.bb.team(opts['<user>']).repositories()
            else:
                raise
        if not repos:
            return
        maxlen = max([len(x.name) for x in repos])
        fmt = u"%%-%ds %%5s %%s" % maxlen
        for repo in repos:
            color = [attr.normal]
            if repo.is_private:
                color.append(fgcolor.red)
            if 'parent' in repo.data:
                if opts['--no-forks']:
                    continue
                color.append(attr.faint)
            print(wrap(fmt % (repo.name, '(%s)' % repo.scm, repo.description), *color))

    @command
    @wants_root
    def setup_goblet(self, opts):
        """\nSet up goblet config based on Bitbucket config"""
        tmpOpts = dict(opts)
        tmpOpts['--root'] = False
        repo = self.repository(tmpOpts)
        root = self.repository(opts)
        self.gitm('config', 'goblet.owner', root.owner['display_name'] or root.owner['username'])
        self.gitm('config', 'goblet.cloneurlhttp', repo.links['clone']['https'])
        self.gitm('config', 'goblet.cloneurlssh', repo.links['clone']['ssh'])
        if repo.description:
            with open(os.path.join(self.gitm('rev-parse', '--git-dir').stdout.strip(), 'description'), 'w') as fd:
                fd.write(repo.description)

    @command
    def set_origin(self, opts, repo=None, remote='origin'):
        """[--ssh|--http] [--triangular [--upstream-branch=<branch>]]
           Set the remote 'origin' to github.
           If this is a fork, set the remote 'upstream' to the parent"""
        if not repo:
            repo = self.repository(opts)
            # Is this mine? No? Do I have a clone?
            if repo.owner['username'] != self.my_login:
                try:
                    repo = self.me.repository(repo.slug)
                except bbapi.BitBucketError:
                    pass

        url = self.clone_url(repo, opts)
        if self.git('config', 'remote.%s.url' % remote).stdout.strip() != url:
            print("Pointing %s to %s" % (remote, url))
            self.gitm('config', 'remote.%s.url' % remote, url)
        self.gitm('config', '--replace-all', 'remote.%s.fetch' % remote, '+refs/heads/*:refs/remotes/%s/*' % remote)

        if repo.is_fork:
            parent = self.bb.repository(repo.fork_of['owner'], repo.fork_of['slug'])
            url = self.clone_url(parent, opts)
            if self.git('config', 'remote.upstream.url').stdout.strip() != url:
                print("Pointing upstream to %s" % url)
                self.gitm('config', 'remote.upstream.url', url)
            self.gitm('config', 'remote.upstream.fetch', '+refs/heads/*:refs/remotes/upstream/*')

        if self.git('ls-remote', remote).stdout.strip():
            self.gitm('fetch', '--prune', '--tags', remote, redirect=False)
        if repo.is_fork:
            self.gitm('fetch', '--prune', '--tags', 'upstream', redirect=False)

        if remote != 'origin':
            return

        self.set_tracking_branches(remote, upstream="upstream", triangular=opts['--triangular'], upstream_branch=opts['--upstream-branch'])

    @command
    def snippet(self, opts):
        """[--description=<description>] <file>...
           Create a new snippet from files or stdin"""
        files = {}
        description = opts['--description'] or ''
        for f in opts['<file>']:
            if f == '-':
                files['stdout'] = sys.stdin.read()
            else:
                if not os.path.exists(f):
                    err("No such file: %s" % f)
                with open(f) as fd:
                    files[os.path.basename(f)] = fd.read()
        snippet = self.me.create_snippet(description=description, files=files)
        print("Snippet created at %s" % snippet.links['html']['href'])

    @command
    def snippets(self, opts):
        """[<user>]
           Show all snippets for a user"""
        snippets = self.bb.user(opts['<user>'] or self.my_login).snippets()
        for snippet in snippets:
            print("%s - %s" % (snippet.title, snippet.links['html']['href']))

    @command
    def whoami(self, opts):
        """\nDisplay BitBucket user info"""
        opts['<user>'] = [self.my_login]
        self.whois(opts)

    @command
    def whois(self, opts):
        """<user>...
           Display GitHub user info"""
        for user_ in opts['<user>']:
            try:
                user = self.bb.user(user_)
            except:
                if 'is a team account' in str(sys.exc_info()[1]):
                    user = self.bb.team(user_)
                else:
                    print("No such user: %s" % user_)
                    continue
            print(wrap(user.display_name or user.username, attr.bright, attr.underline))
            print("Profile:  %s" % user.links['html']['href'])
            if hasattr(user, 'website') and user.website:
                print("Website:  %s" % user.website)
            if hasattr(user, 'location') and user.location:
                print("Location: %s" % user.location)
            try:
                keys = user.keys()
            except bbapi.BitBucketError:
                keys = []
            for pkey in keys:
                algo, key = pkey.key.split()[:2]
                algo = algo[4:].upper()
                if pkey.label:
                    print("%s key%s...%s (%s)" % (algo, ' ' * (6 - len(algo)), key[-10:], pkey.label))
                else:
                    print("%s key%s...%s" % (algo, ' ' * (6 - len(algo)), key[-10:]))
            if user.username == self.my_login:
                teams = [x.username for x in self.bb.teams()]
                if teams:
                    teams.sort()
                    print("Member of %s" % ', '.join(teams))
            if user.type == 'team':
                print('Members:')
                for member in user.members():
                    print(" - %s" % member.username)
                print('Projects:')
                for project in user.projects():
                    print(" - [%s] %s" % (project.key, project.name))
