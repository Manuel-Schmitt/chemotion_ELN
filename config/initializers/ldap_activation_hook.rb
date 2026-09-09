# frozen_string_literal: true

# Registers a synchronous LDAP/AD membership check as a before_create hook, from the outside,
# without touching app/models/user.rb.
#
# All LDAP settings are read from the environment (.env file); leave LDAP_HOST/LDAP_BASE/
# LDAP_ACTIVATION_GROUP_DN blank to disable the whole feature. 
# The feature needs DEVISE_NEW_ACCOUNT_INACTIVE=true to be set in the environment.
module LdapMembershipCheck
  NESTED_GROUP_MATCHING_RULE = '1.2.840.113556.1.4.1941'
  USER_OBJECT_FILTER = '(objectClass=user)(objectCategory=person)'

  def self.enabled?
    ENV['LDAP_HOST'].present? && ENV['LDAP_BASE'].present? && ENV['LDAP_ACTIVATION_GROUP_DN'].present?
  end

  def self.member?(uid)
    return false if uid.blank?

    filter = membership_filter(Net::LDAP::Filter.escape(uid))
    search(filter: filter, attributes: ['dn']).present?
  end

  # Single-query to return all users of LDAP_ACTIVATION_GROUP_DN as set of lowercase strings.
  def self.members
    attribute = uid_attribute
    entries = search(filter: membership_filter, attributes: [attribute], paged_searches: true)
    entries.filter_map { |entry| entry[attribute]&.first&.downcase }.to_set
  end

  def self.membership_filter(uid_value = nil)
    group_dn = Net::LDAP::Filter.escape(ENV.fetch('LDAP_ACTIVATION_GROUP_DN', nil))
    uid_clause = uid_value ? "(#{uid_attribute}=#{uid_value})" : ''
    Net::LDAP::Filter.construct(
      "(&#{uid_clause}#{USER_OBJECT_FILTER}(memberOf:#{NESTED_GROUP_MATCHING_RULE}:=#{group_dn}))",
    )
  end

  # Raises rather than returning nil, on any connection/search failure.
  def self.search(filter:, attributes:, paged_searches: false)
    conn = connection
    result = conn.search(base: ENV.fetch('LDAP_BASE', nil), filter: filter, attributes: attributes,
                         paged_searches: paged_searches)
    return result unless result.nil?

    raise Net::LDAP::Error, conn.get_operation_result.message
  end

  def self.uid_attribute
    ENV['LDAP_UID_ATTRIBUTE'].presence || 'sAMAccountName'
  end

  def self.connection
    Net::LDAP.new(
      host: ENV.fetch('LDAP_HOST', nil),
      port: ENV['LDAP_PORT'].presence || 389,
      base: ENV.fetch('LDAP_BASE', nil),
      encryption: encryption_options,
      auth: bind_auth,
    )
  end

  def self.bind_auth
    bind_dn = ENV.fetch('LDAP_BIND_DN', nil)
    return { method: :anonymous } if bind_dn.blank?

    { method: :simple, username: bind_dn, password: ENV.fetch('LDAP_BIND_PASSWORD', nil) }
  end

  def self.encryption_options
    case ENV['LDAP_ENCRYPTION_METHOD'].to_s
    when 'ssl' then { method: :simple_tls }
    when 'tls' then { method: :start_tls }
    end
  end
end


Rails.application.config.to_prepare do
  next if User._create_callbacks.any? { |cb| cb.kind == :before && cb.filter == :ldap_activation_check }

  User.before_create(
    :ldap_activation_check,
    if: proc { |user| user.type == 'Person' && !user.account_active? && LdapMembershipCheck.enabled? },
  )

  User.send(:define_method, :ldap_activation_check) do
    account_attribute = ENV['LDAP_ACCOUNT_ATTRIBUTE'].presence || 'name_abbreviation'
    uid = public_send(account_attribute)

    if LdapMembershipCheck.member?(uid)
      self.account_active = true
    else
      errors.add(:base, 'is not a member of the required LDAP group')
      throw :abort
    end
  rescue Net::LDAP::Error => e
    # Block registration rather than silently creating a disabled account we can't verify.
    Rails.logger.error("LDAP group membership lookup failed for #{uid}: #{e.message}")
    errors.add(:base, 'could not be verified against the LDAP directory, please try again later')
    throw :abort
  end
  User.send(:private, :ldap_activation_check)
end
