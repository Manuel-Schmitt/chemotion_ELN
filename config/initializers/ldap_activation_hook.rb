# frozen_string_literal: true

# Registers the LDAP/AD activation hook from the outside, without touching app/models/user.rb.
#
# Intended to be dropped onto an already running production system (e.g. as a hotfix
# initializer): right after a disabled Person account is created (see
# User#set_account_active / ENV['DEVISE_NEW_ACCOUNT_INACTIVE']), it is checked against a
# configured AD/LDAP group and either activated (member) or removed (non-member).
#
# All LDAP settings are read from the environment; leave LDAP_HOST/LDAP_BASE/
# LDAP_ACTIVATION_GROUP_DN blank to disable the whole feature.
class LdapActivationJob < ApplicationJob
  queue_as :default

  def self.enabled?
    ENV['LDAP_HOST'].present? && ENV['LDAP_BASE'].present? && ENV['LDAP_ACTIVATION_GROUP_DN'].present?
  end

  def perform(user_id)
    return unless self.class.enabled?

    user = User.persons.find_by(id: user_id)
    return if user.blank? || user.account_active?

    account_attribute = ENV['LDAP_ACCOUNT_ATTRIBUTE'].presence || 'name_abbreviation'
    uid = user.public_send(account_attribute)

    if member_of_activation_group?(uid)
      user.update!(account_active: true)
    else
      user.destroy!
    end
  rescue Net::LDAP::Error => e
    # Leave the account disabled and retry later rather than deleting it on a transient outage.
    Rails.logger.error("LDAP group membership lookup failed for #{uid}: #{e.message}")
  end

  private

  # @return [Boolean] whether uid exists in the directory and is a member of the configured
  #   group. Raises Net::LDAP::Error if the directory itself is unreachable.
  def member_of_activation_group?(uid)
    return false if uid.blank?

    entry = find_entry(uid)
    return false if entry.blank?

    group_dn = ENV.fetch('LDAP_ACTIVATION_GROUP_DN', nil)
    Array(entry[:memberof]).any? { |dn| dn.to_s.casecmp(group_dn).zero? }
  end

  def find_entry(uid)
    uid_attribute = ENV['LDAP_UID_ATTRIBUTE'].presence || 'sAMAccountName'
    filter = Net::LDAP::Filter.eq(uid_attribute, uid)
    ldap_connection.search(base: ENV.fetch('LDAP_BASE', nil), filter: filter, attributes: ['memberof'])&.first
  end

  def ldap_connection
    Net::LDAP.new(
      host: ENV.fetch('LDAP_HOST', nil),
      port: ENV['LDAP_PORT'].presence || 389,
      base: ENV.fetch('LDAP_BASE', nil),
      encryption: ldap_encryption_options,
      auth: ldap_bind_auth,
    )
  end

  def ldap_bind_auth
    bind_dn = ENV.fetch('LDAP_BIND_DN', nil)
    return { method: :anonymous } if bind_dn.blank?

    { method: :simple, username: bind_dn, password: ENV.fetch('LDAP_BIND_PASSWORD', nil) }
  end

  def ldap_encryption_options
    case ENV['LDAP_ENCRYPTION_METHOD'].to_s
    when 'ssl' then { method: :simple_tls }
    when 'tls' then { method: :start_tls }
    end
  end
end

# `to_prepare` re-registers the callback every time the app is (re)loaded; guard against
# duplicate registration so repeated code reloads in development don't queue the job more
# than once per creation.
Rails.application.config.to_prepare do
  next if User._commit_callbacks.any? { |cb| cb.filter == :ldap_activation_hook }

  User.after_create_commit(
    :ldap_activation_hook,
    if: proc { |user|
      user.type == 'Person' && !user.account_active? && LdapActivationJob.enabled?
    },
  )

  User.send(:define_method, :ldap_activation_hook) do
    LdapActivationJob.perform_later(id)
  end
  User.send(:private, :ldap_activation_hook)
end