# frozen_string_literal: true

# Periodically re-verifies every Person's LDAP/AD group membership (see
# config/initializers/ldap_activation_hook.rb) and flips account_active accordingly: a member
# who is currently disabled gets activated, and a previously-approved member who has since left
# the group gets disabled. Runs every even hour (00:00, 02:00, ... 22:00).
#
# Applies to every Person, not just accounts created through the LDAP-gated registration flow -
# any Person whose LDAP_ACCOUNT_ATTRIBUTE value is no longer a member of LDAP_ACTIVATION_GROUP_DN
# will be disabled on the next sweep.
#
# Self-registers as a delayed_cron_job recurring job (the same mechanism
# config/initializers/delayed_job_config.rb / InitCronJobsJob use for the app's other recurring
# jobs), without touching either of those files.
class LdapPeriodicRecheckJob < ApplicationJob
  queue_as :default

  CRON_SCHEDULE = '0 0-23/2 * * *' # every even hour, on the hour

  def perform
    return unless LdapMembershipCheck.enabled?

    account_attribute = ENV['LDAP_ACCOUNT_ATTRIBUTE'].presence || 'name_abbreviation'
    # One query for the whole group instead of one per user (LdapMembershipCheck.member? would
    # otherwise be called once per Person).
    members = LdapMembershipCheck.members

    User.persons.find_each { |user| sync_account_active!(user, account_attribute, members) }
  rescue Net::LDAP::Error => e
    # The next scheduled run (in at most 2 hours) will retry from scratch.
    Rails.logger.error("LDAP periodic recheck failed: #{e.message}")
  end

  private

  def sync_account_active!(user, account_attribute, members)
    uid = user.public_send(account_attribute).to_s.downcase
    member = members.include?(uid)

    user.update!(account_active: true) if member && !user.account_active?
    user.update!(account_active: false) if !member && user.account_active?
  end
end

# Self-schedule via delayed_cron_job's cron support (the same mechanism InitCronJobsJob uses for
# the app's other recurring jobs), without touching config/initializers/delayed_job_config.rb.
ActiveSupport.on_load(:active_record) do
  next unless ActiveRecord::Base.connection.table_exists?('delayed_jobs') && Delayed::Job.column_names.include?('cron')

  Delayed::Job.where('handler like ?', '%LdapPeriodicRecheckJob%').where.not(cron: nil).destroy_all

  next_run_time = Fugit.parse_cronish(LdapPeriodicRecheckJob::CRON_SCHEDULE)&.next_time&.to_t
  LdapPeriodicRecheckJob.set(wait_until: next_run_time, cron: LdapPeriodicRecheckJob::CRON_SCHEDULE).perform_later
rescue PG::ConnectionBad, ActiveRecord::NoDatabaseError => e
  Rails.logger.warn(e.message)
end
