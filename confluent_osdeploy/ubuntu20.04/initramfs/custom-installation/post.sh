#!/bin/bash
deploycfg=/custom-installation/confluent/confluent.deploycfg
confluent_mgr=$(grep ^deploy_server $deploycfg|awk '{print $2}')
confluent_profile=$(grep ^profile: $deploycfg|awk '{print $2}')
export deploycfg confluent_mgr confluent_profile
confluent_whost=$confluent_mgr
case "$confluent_whost" in \[*\]) ;; *:*) confluent_whost="[$confluent_whost]" ;; esac
curl -f https://$confluent_whost/confluent-public/os/$confluent_profile/scripts/post.sh > /tmp/post.sh
bash /tmp/post.sh
true
