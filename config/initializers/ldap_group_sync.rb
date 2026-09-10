# frozen_string_literal: true

# Keeps Chemotion ELN Group membership in sync with AD/LDAP groups, configured via
# config/ad_group_sync.yml (see config/ad_group_sync.yml.example).
# A mapping to a Chemotion Group that doesn't exist, or an AD group that resolves to zero
# members (indistinguishable from a misconfigured/renamed ad_group_dn, is skipped with a
# warning rather than risking emptying out a Chemotion group.
#
# Self-registers as a delayed_cron_job recurring job.
class LdapGroupSyncJob < ApplicationJob
  queue_as :default

  CRON_SCHEDULE = '05 0-23/2 * * *' # every even hour, offset from LdapPeriodicRecheckJob
  LDAP_DELAY = (ENV['LDAP_GROUP_SYNC_DELAY'].presence ||1.0).to_f

  def self.enabled?
    ENV['LDAP_HOST'].present? && ENV['LDAP_BASE'].present? && mappings.present?
  end

  def self.mappings
    Rails.application.config.ad_group_sync_mappings
  end

  def perform
    return unless self.class.enabled?

    account_attribute = ENV['LDAP_ACCOUNT_ATTRIBUTE'].presence || 'name_abbreviation'
    persons_by_uid = User.persons.each_with_object({}) do |person, hash|
      uid = person.public_send(account_attribute).to_s.downcase
      hash[uid] = person if uid.present?
    end

    self.class.mappings.each_with_index do |mapping, index|
      pace_ldap_calls(index)
      sync_mapping!(mapping, persons_by_uid)
    end
  rescue Net::LDAP::Error => e
    Rails.logger.error("LDAP group sync failed: #{e.message}")
  end

  private

  def pace_ldap_calls(index)
    sleep(LDAP_DELAY) if index.positive? && LDAP_DELAY.positive?
  end

  def sync_mapping!(mapping, persons_by_uid)
    group = find_chemotion_group(mapping)
    return if group.blank?

    ad_uids = find_ad_uids(mapping)
    return if ad_uids.blank?

    apply_membership!(group, ad_uids.filter_map { |uid| persons_by_uid[uid] })
  end

  def find_chemotion_group(mapping)
    group = Group.find_by(name_abbreviation: mapping[:chemotion_group])
    Rails.logger.warn("LdapGroupSyncJob: no Chemotion Group '#{mapping[:chemotion_group]}', skipping") if group.blank?
    group
  end

  # A successful-but-empty AD result is indistinguishable from a misconfigured/renamed
  # ad_group_dn; skip rather than risk emptying out the Chemotion group.
  def find_ad_uids(mapping)
    ad_uids = LdapMembershipCheck.members(mapping[:ad_group_dn])
    if ad_uids.blank?
      Rails.logger.warn(
        "LdapGroupSyncJob: AD group '#{mapping[:ad_group_dn]}' returned no members, " \
        "skipping #{mapping[:chemotion_group]}",
      )
    end
    ad_uids
  end

  def apply_membership!(group, desired)
    current = group.users.where(type: 'Person').to_a

    to_add = desired - current
    to_remove = current - desired

    group.users << to_add if to_add.any?
    group.users.delete(to_remove) if to_remove.any?
  end
end

# Loads config/ad_group_sync.yml (copying it from the .example on first boot.
# Failing closed on error (no mappings -> sync disabled).
begin
  unless File.exist?(ad_group_sync_config = Rails.root.join('config', 'ad_group_sync.yml'))
    FileUtils.cp(Rails.root.join('config', 'ad_group_sync.yml.example'), ad_group_sync_config)
  end
  ad_group_sync_settings = Rails.application.config_for(:ad_group_sync)

  Rails.application.configure do
    config.ad_group_sync_mappings = ad_group_sync_settings&.dig(:mappings) || []
  end
rescue StandardError => e
  Rails.logger.error("ad_group_sync: failed to load configuration (#{e.message}); sync disabled")
  Rails.application.configure do
    config.ad_group_sync_mappings = []
  end
end

# Self-schedule via delayed_cron_job's cron support.
ActiveSupport.on_load(:active_record) do
  next unless ActiveRecord::Base.connection.table_exists?('delayed_jobs') && Delayed::Job.column_names.include?('cron')

  Delayed::Job.where('handler like ?', '%LdapGroupSyncJob%').where.not(cron: nil).destroy_all

  next_run_time = Fugit.parse_cronish(LdapGroupSyncJob::CRON_SCHEDULE)&.next_time&.to_t
  LdapGroupSyncJob.set(wait_until: next_run_time, cron: LdapGroupSyncJob::CRON_SCHEDULE).perform_later
rescue PG::ConnectionBad, ActiveRecord::NoDatabaseError => e
  Rails.logger.warn(e.message)
end
