# frozen_string_literal: true

# Periodically re-verifies every Person's LDAP/AD group membership and flips account_active accordingly:
# Applies to every Person
# Self-registers as a delayed_cron_job recurring job.
class LdapPeriodicRecheckJob < ApplicationJob
  queue_as :default

  CRON_SCHEDULE = '0 0-23/2 * * *' # every even hour, on the hour

  def perform
    return unless LdapMembershipCheck.enabled? && ENV['LDAP_ACTIVATION_GROUP_DN'].present?

    account_attribute = ENV['LDAP_ACCOUNT_ATTRIBUTE'].presence || 'name_abbreviation'
    members = LdapMembershipCheck.members(ENV.fetch('LDAP_ACTIVATION_GROUP_DN'))
    if members.blank?
      # A successful-but-empty result is indistinguishable from a misconfigured/renamed
      # LDAP_ACTIVATION_GROUP_DN; skip rather than risk deactivating every active Person.
      Rails.logger.warn('LDAP periodic recheck: group query returned no members, skipping this run')
      return
    end

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

# Self-schedule via delayed_cron_job's cron support
ActiveSupport.on_load(:active_record) do
  next unless ActiveRecord::Base.connection.table_exists?('delayed_jobs') && Delayed::Job.column_names.include?('cron')

  Delayed::Job.where('handler like ?', '%LdapPeriodicRecheckJob%').where.not(cron: nil).destroy_all

  next_run_time = Fugit.parse_cronish(LdapPeriodicRecheckJob::CRON_SCHEDULE)&.next_time&.to_t
  LdapPeriodicRecheckJob.set(wait_until: next_run_time, cron: LdapPeriodicRecheckJob::CRON_SCHEDULE).perform_later
rescue PG::ConnectionBad, ActiveRecord::NoDatabaseError => e
  Rails.logger.warn(e.message)
end
