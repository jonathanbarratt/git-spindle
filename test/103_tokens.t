#!/bin/sh

test_description="Creating personal access tokens"

. ./setup.sh

export DEBUG=1

test_expect_success hub "Creating personal access token" "
        password=\$(git config -f .gitspindle testsuite.github-test-1.password) &&
        test -n \"\$password\" &&
        echo \"\$password\" | git_hub_1 create-token > actual &&
        grep 'access token is: ..........' actual
"

test_expect_success hub "Creating personal access token and storing it in a credential helper" "
        apitoken=\$(git_hub_1 config token) &&
        git config --global credential.helper store &&
        git_hub_1 config token \$apitoken &&
        password=\$(git config -f .gitspindle testsuite.github-test-1.password) &&
        test -n \"\$password\" &&
        echo \"\$password\" | git_hub_1 create-token --store > actual &&
        token=\$(sed -ne 's/.*: //p' actual) &&
        echo \"https://$(username git_hub_1):\$token@$(spindle_host git_hub_1)\" > expected &&
        echo \"https://$(username git_hub_1):\$apitoken@api.$(spindle_host git_hub_1)\" >> expected &&
        test_cmp expected ~/.git-credentials
"

#TODO Password shouldn't be requested two times
test_expect_success hub,2fa,INTERACTIVE "Creating personal access token (2fa user)" "
        password=\$(git config -f .gitspindle testsuite.github-test-3.password) &&
        echo -n 'Two-Factor Authentication Code: ' &&
        read otp &&
        (echo \"\$password\"; echo \"\$otp\"; echo \"\$password\" ) | git_hub_3 create-token
"

test_done

# vim: set syntax=sh:
