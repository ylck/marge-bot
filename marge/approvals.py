import logging as log
import fnmatch
import shlex

from . import gitlab

GET, POST, PUT = gitlab.GET, gitlab.POST, gitlab.PUT


class Approvals(gitlab.Resource):
    """Approval info for a MergeRequest."""

    def refetch_info(self):
        gitlab_version = self._api.version()
        if gitlab_version.release >= (9, 2, 2):
            approver_url = '/projects/{0.project_id}/merge_requests/{0.iid}/approvals'.format(self)
        else:
            # GitLab botched the v4 api before 9.2.3
            approver_url = '/projects/{0.project_id}/merge_requests/{0.id}/approvals'.format(self)

        if gitlab_version.is_ee:
            self._info = self._api.call(GET(approver_url))
        else:
            self._info = self.get_approvers_ce()

    def get_approvers_ce(self):
        """get approvers status using thumbs on merge request
        """
        owner_globs = self.get_codeowners_ce()
        if not owner_globs:
            log.info("No CODEOWNERS file in master, continuing without approvers flow")
            return dict(self._info, approvals_left=0, approved_by=[], codeowners=[])

        codeowners = self.determine_responsible_owners(owner_globs, self.get_changes_ce())

        if not codeowners:
            log.info("No matched code owners, continuing without approvers flow")
            return dict(self._info, approvals_left=0, approved_by=[], codeowners=[])

        awards = self.get_awards_ce()

        up_votes = [e for e in awards if e['name'] == 'thumbsup' and e['user']['username'] in codeowners]
        approver_count = len(codeowners)
        approvals_left = max(approver_count - len(up_votes), 0)

        return dict(self._info, approvals_left=approvals_left, approved_by=up_votes, codeowners=codeowners)

    def determine_responsible_owners(self, owners_glob, changes):
        owners = set([])

        # Always add global users
        if '*' in owners_glob:
            owners.update(owners_glob['*'])

        if 'changes' not in changes:
            log.info("No changes in merge request!?")
            return owners

        for change in changes['changes']:
            for glob, users in owners_glob.items():
                if 'new_path' in change and fnmatch.fnmatch(change['new_path'], glob):
                    owners.update(users)

        return owners

    def get_changes_ce(self):
        changes_url = '/projects/{0.project_id}/merge_requests/{0.iid}/changes'
        changes_url = changes_url.format(self)

        return self._api.call(GET(changes_url))

    def get_awards_ce(self):
        emoji_url = '/projects/{0.project_id}/merge_requests/{0.iid}/award_emoji'
        emoji_url = emoji_url.format(self)
        return self._api.call(GET(emoji_url))

    def get_codeowners_ce(self):
        config_file = self._api.repo_file_get(self.project_id, "CODEOWNERS", "master")
        owner_globs = {}

        if config_file is None:
            return owner_globs

        for line in config_file['content'].splitlines():
            if line != "" and not line.startswith(' ') and not line.startswith('#'):
                elements = shlex.split(line)
                glob = elements.pop(0)
                owner_globs.setdefault(glob, set([]))

                for user in elements:
                    owner_globs[glob].add(user.strip('@'))

        return owner_globs

    @property
    def iid(self):
        return self.info['iid']

    @property
    def project_id(self):
        return self.info['project_id']

    @property
    def approvals_left(self):
        return self.info['approvals_left'] or 0

    @property
    def sufficient(self):
        return not self.info['approvals_left']

    @property
    def approver_usernames(self):
        return [who['user']['username'] for who in self.info['approved_by']]

    @property
    def approver_ids(self):
        """Return the uids of the approvers."""
        return [who['user']['id'] for who in self.info['approved_by']]

    @property
    def codeowners(self):
        """Only used for gitlab CE"""
        if 'approvers' in self.info:
            return self.info['codeowners']

        return []

    def reapprove(self):
        """Impersonates the approvers and re-approves the merge_request as them.

        The idea is that we want to get the approvers, push the rebased branch
        (which may invalidate approvals, depending on GitLab settings) and then
        restore the approval status.
        """
        gitlab_version = self._api.version()

        if gitlab_version.release >= (9, 2, 2):
            approve_url = '/projects/{0.project_id}/merge_requests/{0.iid}/approve'.format(self)
        else:
            # GitLab botched the v4 api before 9.2.3
            approve_url = '/projects/{0.project_id}/merge_requests/{0.id}/approve'.format(self)

        if gitlab_version.is_ee:
            for uid in self.approver_ids:
                self._api.call(POST(approve_url), sudo=uid)
